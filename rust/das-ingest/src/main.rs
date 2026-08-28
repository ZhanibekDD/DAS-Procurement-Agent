use calamine::{Data, Reader, open_workbook_auto};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::error::Error;
use std::fmt::{Display, Formatter};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

const MAX_FILE_BYTES: u64 = 25 * 1024 * 1024;
const MAX_OUTPUT_ITEMS: usize = 10_000;

#[derive(Debug)]
struct IngestError(String);

impl Display for IngestError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for IngestError {}

#[derive(Debug)]
struct Arguments {
    input: PathBuf,
    filename: String,
    pretty: bool,
}

#[derive(Debug, Serialize)]
struct IngestResult {
    schema_version: &'static str,
    source: SourceInfo,
    elapsed_ms: u128,
    sheets: Vec<SheetInfo>,
    suppliers: Vec<Supplier>,
    summaries: Vec<SupplierSummary>,
    line_items: Vec<LineItem>,
    warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
struct SourceInfo {
    filename: String,
    format: String,
    size_bytes: u64,
    sha256: String,
    currency: String,
}

#[derive(Debug, Serialize)]
struct SheetInfo {
    name: String,
    rows: usize,
    columns: usize,
    non_empty_cells: usize,
}

#[derive(Debug, Clone, Serialize)]
struct Supplier {
    name: String,
    normalized_name: String,
}

#[derive(Debug, Serialize)]
struct SupplierSummary {
    sheet: String,
    section: String,
    supplier: String,
    work_total: Option<f64>,
    materials_project_total: Option<f64>,
    materials_analogue_total: Option<f64>,
    total_project: Option<f64>,
    total_analogue: Option<f64>,
    total: Option<f64>,
    vat: String,
    notes: Vec<String>,
    source_row: usize,
}

#[derive(Debug, Serialize)]
struct LineItem {
    sheet: String,
    section: String,
    supplier: Option<String>,
    name: String,
    specification: String,
    unit: String,
    quantity: Option<f64>,
    unit_price: Option<f64>,
    total: Option<f64>,
    source_row: usize,
    confidence: f64,
}

#[derive(Default)]
struct ParsedWorkbook {
    sheets: Vec<SheetInfo>,
    summaries: Vec<SupplierSummary>,
    line_items: Vec<LineItem>,
    suppliers: BTreeMap<String, Supplier>,
    warnings: Vec<String>,
    currency_markers: BTreeSet<String>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("das-ingest: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let arguments = parse_arguments()?;
    let started = Instant::now();
    let metadata = fs::metadata(&arguments.input)?;
    if !metadata.is_file() {
        return Err(IngestError("input must be a regular file".into()).into());
    }
    if metadata.len() == 0 || metadata.len() > MAX_FILE_BYTES {
        return Err(IngestError("input must be between 1 byte and 25 MB".into()).into());
    }
    let bytes = fs::read(&arguments.input)?;
    let extension = Path::new(&arguments.filename)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let mut parsed = match extension.as_str() {
        "xlsx" => {
            if !bytes.starts_with(b"PK") {
                return Err(IngestError("invalid XLSX payload".into()).into());
            }
            parse_xlsx(&arguments.input)?
        }
        "csv" => {
            if bytes.contains(&0) {
                return Err(IngestError("CSV payload contains NUL bytes".into()).into());
            }
            parse_csv(&bytes)?
        }
        _ => return Err(IngestError("only .xlsx and .csv are supported".into()).into()),
    };
    if parsed.line_items.len() >= MAX_OUTPUT_ITEMS {
        parsed.warnings.push(format!(
            "line items truncated at {MAX_OUTPUT_ITEMS} rows; split the workbook before import"
        ));
    }

    let result = IngestResult {
        schema_version: "1.0",
        source: SourceInfo {
            filename: safe_filename(&arguments.filename),
            format: extension,
            size_bytes: metadata.len(),
            sha256: format!("{:x}", Sha256::digest(&bytes)),
            currency: detect_currency(&parsed.currency_markers),
        },
        elapsed_ms: started.elapsed().as_millis(),
        sheets: parsed.sheets,
        suppliers: parsed.suppliers.into_values().collect(),
        summaries: parsed.summaries,
        line_items: parsed.line_items,
        warnings: parsed.warnings,
    };
    if arguments.pretty {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!("{}", serde_json::to_string(&result)?);
    }
    Ok(())
}

fn parse_arguments() -> Result<Arguments, IngestError> {
    let mut values = env::args().skip(1);
    let mut input = None;
    let mut filename = None;
    let mut pretty = false;
    while let Some(value) = values.next() {
        match value.as_str() {
            "--input" => input = values.next().map(PathBuf::from),
            "--filename" => filename = values.next(),
            "--pretty" => pretty = true,
            "--help" | "-h" => {
                println!("Usage: das-ingest --input PATH --filename NAME.xlsx [--pretty]");
                std::process::exit(0);
            }
            _ => return Err(IngestError(format!("unknown argument: {value}"))),
        }
    }
    let input = input.ok_or_else(|| IngestError("--input is required".into()))?;
    let filename = filename
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            input
                .file_name()
                .map(|value| value.to_string_lossy().into_owned())
        })
        .ok_or_else(|| IngestError("--filename is required".into()))?;
    Ok(Arguments {
        input,
        filename,
        pretty,
    })
}

fn parse_xlsx(path: &Path) -> Result<ParsedWorkbook, Box<dyn Error>> {
    let mut workbook = open_workbook_auto(path)?;
    let sheet_names = workbook.sheet_names().to_vec();
    let mut parsed = ParsedWorkbook::default();
    for sheet_name in sheet_names {
        let range = workbook.worksheet_range(&sheet_name)?;
        let rows = range
            .rows()
            .map(|row| row.iter().map(cell_text).collect())
            .collect();
        consume_sheet(&mut parsed, &sheet_name, rows);
    }
    add_empty_result_warning(&mut parsed);
    Ok(parsed)
}

fn parse_csv(bytes: &[u8]) -> Result<ParsedWorkbook, Box<dyn Error>> {
    let text = std::str::from_utf8(bytes)
        .map_err(|_| IngestError("CSV must be UTF-8 encoded".into()))?
        .trim_start_matches('\u{feff}');
    let delimiter = detect_delimiter(text);
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(delimiter)
        .has_headers(false)
        .flexible(true)
        .from_reader(text.as_bytes());
    let mut rows = Vec::new();
    for record in reader.records() {
        rows.push(record?.iter().map(str::to_owned).collect());
    }
    let mut parsed = ParsedWorkbook::default();
    consume_sheet(&mut parsed, "CSV", rows);
    add_empty_result_warning(&mut parsed);
    Ok(parsed)
}

fn add_empty_result_warning(parsed: &mut ParsedWorkbook) {
    if parsed.summaries.is_empty() && parsed.line_items.is_empty() {
        parsed.warnings.push(
            "no supported supplier summary or item table was detected; human mapping is required"
                .into(),
        );
    }
}

fn consume_sheet(parsed: &mut ParsedWorkbook, sheet_name: &str, rows: Vec<Vec<String>>) {
    let columns = rows.iter().map(Vec::len).max().unwrap_or(0);
    let non_empty_cells = rows
        .iter()
        .flat_map(|row| row.iter())
        .filter(|value| !value.trim().is_empty())
        .count();
    collect_currency_markers(parsed, &rows);
    parsed.sheets.push(SheetInfo {
        name: sheet_name.to_owned(),
        rows: rows.len(),
        columns,
        non_empty_cells,
    });
    parse_summary_tables(parsed, sheet_name, &rows);
    parse_line_item_table(parsed, sheet_name, &rows);
}

fn parse_summary_tables(parsed: &mut ParsedWorkbook, sheet_name: &str, rows: &[Vec<String>]) {
    for (row_index, row) in rows.iter().enumerate() {
        let first = normalize(row.first().map(String::as_str).unwrap_or(""));
        if first != "подрядчик" && first != "поставщик" {
            continue;
        }
        let section = previous_title(rows, row_index);
        let suppliers: Vec<(usize, String)> = row
            .iter()
            .enumerate()
            .skip(1)
            .filter_map(|(column, value)| {
                let value = clean_supplier_name(value);
                (!value.is_empty()).then_some((column, value))
            })
            .collect();
        if suppliers.is_empty() {
            continue;
        }
        for (_, supplier) in &suppliers {
            register_supplier(parsed, supplier);
        }
        let mut summaries: Vec<SupplierSummary> = suppliers
            .iter()
            .map(|(_, supplier)| SupplierSummary {
                sheet: sheet_name.to_owned(),
                section: section.clone(),
                supplier: supplier.clone(),
                work_total: None,
                materials_project_total: None,
                materials_analogue_total: None,
                total_project: None,
                total_analogue: None,
                total: None,
                vat: String::new(),
                notes: Vec::new(),
                source_row: row_index + 1,
            })
            .collect();

        let end = usize::min(rows.len(), row_index + 22);
        for metric_row in &rows[row_index + 1..end] {
            let label = normalize(metric_row.first().map(String::as_str).unwrap_or(""));
            if label == "подрядчик" || label == "поставщик" {
                break;
            }
            for (summary_index, (column, _)) in suppliers.iter().enumerate() {
                let raw_value = metric_row.get(*column).map(String::as_str).unwrap_or("");
                if raw_value.trim().is_empty() {
                    continue;
                }
                let number = parse_number(raw_value);
                let summary = &mut summaries[summary_index];
                if label.contains("цена работ") || label == "работы" {
                    summary.work_total = number;
                } else if label.contains("материал") && label.contains("проект") {
                    summary.materials_project_total = number;
                } else if label.contains("материал") && label.contains("аналог") {
                    summary.materials_analogue_total = number;
                } else if label.contains("итого") && label.contains("проект") {
                    summary.total_project = number;
                } else if label.contains("итого") && label.contains("аналог") {
                    summary.total_analogue = number;
                } else if label == "итого" || label.contains("итого общ") {
                    summary.total = number;
                } else if label == "ндс" || label.contains("налог") {
                    summary.vat = raw_value.trim().to_owned();
                } else if ["опыт", "комментар", "услов", "срок", "оплат", "достав"]
                    .iter()
                    .any(|marker| label.contains(marker))
                {
                    summary.notes.push(format!(
                        "{}: {}",
                        metric_row.first().map(String::as_str).unwrap_or("").trim(),
                        raw_value.trim()
                    ));
                }
            }
        }
        parsed
            .summaries
            .extend(summaries.into_iter().filter(|summary| {
                summary.work_total.is_some()
                    || summary.materials_project_total.is_some()
                    || summary.materials_analogue_total.is_some()
                    || summary.total_project.is_some()
                    || summary.total_analogue.is_some()
                    || summary.total.is_some()
                    || !summary.vat.is_empty()
                    || !summary.notes.is_empty()
            }));
    }
}

fn parse_line_item_table(parsed: &mut ParsedWorkbook, sheet_name: &str, rows: &[Vec<String>]) {
    let Some(header) = detect_item_header(rows) else {
        return;
    };
    let supplier = infer_single_supplier(rows, header.row_index);
    if let Some(value) = &supplier {
        register_supplier(parsed, value);
    }
    if header.matrix_prices {
        parsed.warnings.push(format!(
            "sheet '{sheet_name}' contains a multi-supplier price matrix; positions were extracted without assigning ambiguous prices"
        ));
    }

    let mut section = String::new();
    let mut consecutive_empty = 0;
    for (row_index, row) in rows.iter().enumerate().skip(header.data_row_index) {
        if parsed.line_items.len() >= MAX_OUTPUT_ITEMS {
            return;
        }
        let name = row
            .get(header.name_column)
            .map(String::as_str)
            .unwrap_or("")
            .trim();
        if name.is_empty() {
            consecutive_empty += 1;
            if consecutive_empty >= 8 {
                break;
            }
            continue;
        }
        consecutive_empty = 0;
        if normalize(name).starts_with("итого") {
            continue;
        }
        let quantity = header
            .quantity_column
            .and_then(|column| row.get(column))
            .and_then(|value| parse_number(value));
        let unit_price = (!header.matrix_prices)
            .then_some(header.unit_price_column)
            .flatten()
            .and_then(|column| row.get(column))
            .and_then(|value| parse_number(value));
        let total = (!header.matrix_prices)
            .then_some(header.total_column)
            .flatten()
            .and_then(|column| row.get(column))
            .and_then(|value| parse_number(value));
        if quantity.is_none() && unit_price.is_none() && total.is_none() {
            if looks_like_section(name) {
                section = name.to_owned();
            }
            continue;
        }
        let specification = header
            .specification_column
            .and_then(|column| row.get(column))
            .map(|value| value.trim().to_owned())
            .unwrap_or_default();
        let unit = header
            .unit_column
            .and_then(|column| row.get(column))
            .map(|value| value.trim().to_owned())
            .unwrap_or_default();
        parsed.line_items.push(LineItem {
            sheet: sheet_name.to_owned(),
            section: section.clone(),
            supplier: supplier.clone(),
            name: name.to_owned(),
            specification,
            unit,
            quantity,
            unit_price,
            total,
            source_row: row_index + 1,
            confidence: if header.matrix_prices { 0.78 } else { 0.9 },
        });
    }
}

struct ItemHeader {
    row_index: usize,
    data_row_index: usize,
    name_column: usize,
    specification_column: Option<usize>,
    unit_column: Option<usize>,
    quantity_column: Option<usize>,
    unit_price_column: Option<usize>,
    total_column: Option<usize>,
    matrix_prices: bool,
}

fn detect_item_header(rows: &[Vec<String>]) -> Option<ItemHeader> {
    for row_index in 0..usize::min(rows.len(), 35) {
        let columns = rows[row_index..usize::min(rows.len(), row_index + 3)]
            .iter()
            .map(Vec::len)
            .max()
            .unwrap_or(0);
        let headers: Vec<String> = (0..columns)
            .map(|column| {
                normalize(
                    &(row_index..usize::min(rows.len(), row_index + 3))
                        .filter_map(|row| rows[row].get(column))
                        .map(String::as_str)
                        .collect::<Vec<_>>()
                        .join(" "),
                )
            })
            .collect();
        let name_column = headers
            .iter()
            .position(|value| value.contains("наименование") || value.contains("название позиции"));
        let unit_column = headers.iter().position(|value| {
            value == "ед" || value.contains("ед изм") || value.contains("единица измер")
        });
        let quantity_column = headers.iter().position(|value| {
            value.contains("кол во") || value.contains("количество") || value.contains("объем")
        });
        if name_column.is_none() || (unit_column.is_none() && quantity_column.is_none()) {
            continue;
        }
        let price_columns: Vec<usize> = headers
            .iter()
            .enumerate()
            .filter_map(|(column, value)| {
                (value.contains("цена за ед")
                    || value.contains("стоимость ед")
                    || value.contains("ст ть единицы"))
                .then_some(column)
            })
            .collect();
        let total_columns: Vec<usize> = headers
            .iter()
            .enumerate()
            .filter_map(|(column, value)| {
                (value.contains("сумма")
                    || value.contains("итого")
                    || value.contains("сметная стоимость"))
                .then_some(column)
            })
            .collect();
        return Some(ItemHeader {
            row_index,
            data_row_index: detect_header_end(rows, row_index),
            name_column: name_column.unwrap_or(0),
            specification_column: headers.iter().position(|value| {
                value.contains("тип марка")
                    || value.contains("характерист")
                    || value.contains("спецификац")
            }),
            unit_column,
            quantity_column,
            unit_price_column: price_columns.first().copied(),
            total_column: total_columns.first().copied(),
            matrix_prices: price_columns.len() > 1 || total_columns.len() > 1,
        });
    }
    None
}

fn detect_header_end(rows: &[Vec<String>], row_index: usize) -> usize {
    let mut end = usize::min(rows.len(), row_index + 1);
    for candidate in row_index + 1..usize::min(rows.len(), row_index + 3) {
        let values: Vec<String> = rows[candidate]
            .iter()
            .map(|value| normalize(value))
            .filter(|value| !value.is_empty())
            .collect();
        if values.is_empty() {
            continue;
        }
        let header_markers = [
            "п п",
            "видов работ",
            "изм",
            "единицы",
            "стоимость",
            "цена",
            "сумма",
            "итого",
        ];
        let headerish = values.iter().all(|value| {
            value.chars().all(|character| character.is_ascii_digit())
                || header_markers.iter().any(|marker| value.contains(marker))
        });
        if !headerish {
            break;
        }
        end = candidate + 1;
    }
    end
}

fn infer_single_supplier(rows: &[Vec<String>], before_row: usize) -> Option<String> {
    let mut candidates = Vec::new();
    for row in &rows[..usize::min(rows.len(), before_row + 1)] {
        for value in row {
            let cleaned = clean_supplier_name(value);
            let normalized = normalize(&cleaned);
            if normalized.starts_with("ооо ")
                || normalized.starts_with("тоо ")
                || normalized.starts_with("ип ")
                || normalized.starts_with("ао ")
                || normalize(value).starts_with("подрядчик ")
            {
                candidates.push(cleaned);
            }
        }
    }
    candidates.sort();
    candidates.dedup();
    (candidates.len() == 1).then(|| candidates.remove(0))
}

fn register_supplier(parsed: &mut ParsedWorkbook, supplier: &str) {
    let cleaned = clean_supplier_name(supplier);
    let normalized_name = normalize(&cleaned);
    if cleaned.is_empty() || normalized_name.is_empty() {
        return;
    }
    parsed
        .suppliers
        .entry(normalized_name.clone())
        .or_insert(Supplier {
            name: cleaned,
            normalized_name,
        });
}

fn cell_text(value: &Data) -> String {
    match value {
        Data::Empty => String::new(),
        Data::String(value) => value.clone(),
        Data::Float(value) => {
            if value.fract() == 0.0 {
                format!("{value:.0}")
            } else {
                value.to_string()
            }
        }
        Data::Int(value) => value.to_string(),
        Data::Bool(value) => value.to_string(),
        Data::Error(value) => format!("{value:?}"),
        other => other.to_string(),
    }
}

fn parse_number(value: &str) -> Option<f64> {
    let mut cleaned = value
        .replace(' ', "")
        .replace('\u{00a0}', "")
        .replace('₽', "")
        .replace('₸', "")
        .replace('$', "")
        .replace('€', "")
        .replace("руб.", "")
        .replace("руб", "")
        .replace("тг", "")
        .trim()
        .to_owned();
    if cleaned.is_empty() {
        return None;
    }
    if cleaned.contains(',') && cleaned.contains('.') {
        if cleaned.rfind(',') > cleaned.rfind('.') {
            cleaned = cleaned.replace('.', "").replace(',', ".");
        } else {
            cleaned = cleaned.replace(',', "");
        }
    } else if cleaned.contains(',') {
        cleaned = cleaned.replace(',', ".");
    }
    cleaned.parse().ok()
}

fn normalize(value: &str) -> String {
    let mut result = String::with_capacity(value.len());
    let mut previous_space = true;
    for character in value.trim().to_lowercase().replace('ё', "е").chars() {
        let mapped = if character.is_alphanumeric() {
            character
        } else {
            ' '
        };
        if mapped == ' ' {
            if !previous_space {
                result.push(' ');
            }
            previous_space = true;
        } else {
            result.push(mapped);
            previous_space = false;
        }
    }
    result.trim().to_owned()
}

fn clean_supplier_name(value: &str) -> String {
    let trimmed = value.trim();
    trimmed
        .strip_prefix("Подрядчик:")
        .or_else(|| trimmed.strip_prefix("Поставщик:"))
        .unwrap_or(trimmed)
        .trim_matches(|character: char| character == ':' || character == '-')
        .trim()
        .to_owned()
}

fn previous_title(rows: &[Vec<String>], row_index: usize) -> String {
    for row in rows[..row_index].iter().rev().take(5) {
        if let Some(value) = row.first().filter(|value| !value.trim().is_empty()) {
            return value.trim().to_owned();
        }
    }
    String::new()
}

fn looks_like_section(value: &str) -> bool {
    let normalized = normalize(value);
    normalized.starts_with("раздел")
        || normalized.starts_with("секция")
        || normalized.ends_with("работы")
        || normalized.ends_with("изделия")
        || normalized.contains("сигнализация")
}

fn collect_currency_markers(parsed: &mut ParsedWorkbook, rows: &[Vec<String>]) {
    for value in rows.iter().flat_map(|row| row.iter()) {
        let normalized = normalize(value);
        if value.contains('₽') || normalized.contains("руб") {
            parsed.currency_markers.insert("RUB".into());
        }
        if value.contains('₸') || normalized.contains("тенге") || normalized.contains("тг")
        {
            parsed.currency_markers.insert("KZT".into());
        }
        if value.contains('$') || normalized.contains("usd") {
            parsed.currency_markers.insert("USD".into());
        }
        if value.contains('€') || normalized.contains("eur") {
            parsed.currency_markers.insert("EUR".into());
        }
    }
}

fn detect_currency(markers: &BTreeSet<String>) -> String {
    if markers.len() == 1 {
        markers.iter().next().cloned().unwrap_or_default()
    } else if markers.is_empty() {
        "UNKNOWN".into()
    } else {
        "MIXED".into()
    }
}

fn detect_delimiter(text: &str) -> u8 {
    let sample = text.lines().take(10).collect::<Vec<_>>().join("\n");
    [
        (b';', sample.matches(';').count()),
        (b'\t', sample.matches('\t').count()),
        (b',', sample.matches(',').count()),
    ]
    .into_iter()
    .max_by_key(|(_, count)| *count)
    .map(|(delimiter, _)| delimiter)
    .unwrap_or(b';')
}

fn safe_filename(value: &str) -> String {
    Path::new(value)
        .file_name()
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|| "spreadsheet".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_localized_numbers() {
        assert_eq!(parse_number("2 395 634,80 ₽"), Some(2_395_634.8));
        assert_eq!(parse_number("931205.3"), Some(931_205.3));
        assert_eq!(parse_number(""), None);
    }

    #[test]
    fn summary_parser_maps_suppliers_and_totals() {
        let rows = vec![
            vec!["КП Электрика".into()],
            vec!["Подрядчик".into(), "ООО Первый".into(), "ТОО Второй".into()],
            vec!["Цена работ".into(), "100 000".into(), "90000".into()],
            vec![
                "Цена материалов по проекту".into(),
                "250000".into(),
                "240000".into(),
            ],
            vec!["НДС".into(), "НДС 12%".into(), "без НДС".into()],
            vec!["Итого по проекту".into(), "350000".into(), "330000".into()],
        ];
        let mut parsed = ParsedWorkbook::default();
        consume_sheet(&mut parsed, "Сводная", rows);
        assert_eq!(parsed.suppliers.len(), 2);
        assert_eq!(parsed.summaries.len(), 2);
        assert_eq!(parsed.summaries[0].total_project, Some(350_000.0));
        assert_eq!(parsed.summaries[1].vat, "без НДС");
    }

    #[test]
    fn line_item_parser_keeps_traceable_rows() {
        let rows = vec![
            vec!["ООО ГрандСтрой".into()],
            vec![
                "№".into(),
                "Наименование".into(),
                "Ед. изм.".into(),
                "Объем".into(),
                "Цена за ед.".into(),
                "Сумма".into(),
            ],
            vec![String::new(); 6],
            vec![String::new(); 6],
            vec![
                "1".into(),
                "Обеспыливание поверхности".into(),
                "м2".into(),
                "1134,26".into(),
                "66".into(),
                "74861,16".into(),
            ],
        ];
        let mut parsed = ParsedWorkbook::default();
        consume_sheet(&mut parsed, "Кровля", rows);
        assert_eq!(parsed.line_items.len(), 1);
        assert_eq!(
            parsed.line_items[0].supplier.as_deref(),
            Some("ООО ГрандСтрой")
        );
        assert_eq!(parsed.line_items[0].source_row, 5);
        assert_eq!(parsed.line_items[0].quantity, Some(1134.26));
    }
}
