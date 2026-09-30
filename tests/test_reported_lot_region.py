import pytest
from procurement.models import ProjectCreate, LotCreate
from test_launch_workflow import workflow, lot_payload


@pytest.mark.parametrize('project_region,lot_region,lot_cluster',[
    ('Новосибирская область','Воронежская область',''),
    ('Новосибирская область','Воронежская область','cluster_2'),
    ('Воронежская область','Новосибирская область',''),
    ('Воронежская область','Новосибирская область','cluster_1'),
    ('Воронежская область','Неопределённый регион','cluster_1'),
])
def test_cross_project_cluster_rejected_before_any_lot_or_audit_write(workflow,project_region,lot_region,lot_cluster):
    db,service,_=workflow
    project=service.create_project(ProjectCreate(name='ТЕСТ регион',region=project_region,delivery_address='Тестовая 1'))
    before={t:db.all('SELECT * FROM '+t) for t in ('lots','lot_items','lot_attachments','audit_log')}
    with pytest.raises(ValueError,match='Регион закупки не соответствует'):
        service.create_lot(LotCreate(**{**lot_payload(project['id']), 'region':lot_region,'cluster':lot_cluster}))
    assert all(db.all('SELECT * FROM '+t)==rows for t,rows in before.items())


@pytest.mark.parametrize('project_region,lot_region',[
    ('Воронежская область','Воронежская область'),
    ('Воронежская область','Краснодарский край'),
    ('Новосибирская область','Тюменская область'),
])
def test_inferred_compatible_cluster_is_saved_and_matching_usable(workflow,project_region,lot_region):
    _,service,_=workflow
    project=service.create_project(ProjectCreate(name='ТЕСТ регион',region=project_region,delivery_address='Тестовая 1'))
    lot=service.create_lot(LotCreate(**{**lot_payload(project['id']), 'region':lot_region}))
    assert lot['cluster']==project['cluster']
    assert service.match_suppliers(lot['id'])==[]
