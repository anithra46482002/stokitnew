import os, sys, uuid
sys.path.insert(0, os.getcwd())
from types import SimpleNamespace
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django; django.setup()
from django.db import transaction, connection
from rest_framework.test import APIClient
from accounts.hashers import verify_php_password
from adminapi.models import DeliveryBoyAdminMetadata
from legacydb.models import DeliveryBoy
class Rollback(Exception): pass
class FixtureRollback(Exception): pass
admin=SimpleNamespace(is_authenticated=True, role='SUPER_ADMIN'); checks=[]
def check(label, ok):
 assert ok, label
 checks.append(label); print('PASS:',label)
def api(method,data=None,query='',auth=True,role='SUPER_ADMIN'):
 path='/admin-api/staff'+(('?'+query) if query else '')
 client=APIClient()
 if auth: client.force_authenticate(user=SimpleNamespace(is_authenticated=True,role=role))
 if method=='GET': res=client.get(path,HTTP_HOST='127.0.0.1')
 elif method=='POST': res=client.post(path,data or {},format='json',HTTP_HOST='127.0.0.1')
 elif method=='PATCH': res=client.patch(path,data or {},format='json',HTTP_HOST='127.0.0.1')
 elif method=='DELETE': res=client.delete(path,data or {},format='json',HTTP_HOST='127.0.0.1')
 else: raise ValueError(method)
 return res
before=list(DeliveryBoy.objects.order_by('id').values_list('id',flat=True))
check('existing Delivery Boy ID 1 present',1 in before)
check('metadata table exists','admin_delivery_boy_metadata' in connection.introspection.table_names())
try:
 with transaction.atomic():
  listed=api('GET',query='kind=delivery&page=1&page_size=20')
  check('live listing HTTP 200',listed.status_code==200)
  check('live listing includes ID 1',any(x['id']==1 for x in listed.data['results']))
  suffix=uuid.uuid4().hex[:10]; username='verify_driver_'+suffix; password='LiveVerifyPassword#2026'
  payload={'kind':'delivery','username':username,'email':username+'@example.invalid','full_name':'Verification Driver','phone':'9876543210','address':'Rollback test','password':password,'confirm_password':password,'status':'ACTIVE'}
  created=api('POST',payload); check('create returns HTTP 201',created.status_code==201)
  did=created.data['id']; check('backend generated ID',isinstance(did,int) and did>1)
  row=DeliveryBoy.objects.get(pk=did); check('Argon2id password hash verifies',row.password.startswith('$argon2id$') and verify_php_password(password,row.password))
  check('password not returned by API','password' not in created.data)
  meta=DeliveryBoyAdminMetadata.objects.get(delivery_boy_id=did); check('ACTIVE persisted in metadata',meta.is_active is True)
  check('Created and Updated dates persisted',row.created_at is not None and meta.updated_at is not None and created.data.get('created_at') and created.data.get('updated_at'))
  initial_created=row.created_at; initial_updated=meta.updated_at
  dupe=api('POST',{**payload,'email':'other@example.invalid'}); check('duplicate username rejected',dupe.status_code==400 and 'username' in dupe.data.get('errors',{}))
  dupe=api('POST',{**payload,'username':username+'_other'}); check('duplicate email rejected',dupe.status_code==400 and 'email' in dupe.data.get('errors',{}))
  mismatch=api('POST',{**payload,'username':username+'_bad','email':'bad-'+payload['email'],'confirm_password':'wrong'})
  check('password confirmation mismatch rejected',mismatch.status_code==400 and 'confirm_password' in mismatch.data)
  old_hash=row.password; edited=api('PATCH',{'kind':'delivery','id':did,'full_name':'Updated Verification Driver','status':'INACTIVE'})
  row.refresh_from_db(); meta.refresh_from_db(); check('edit without password succeeds and preserves hash',edited.status_code==200 and row.password==old_hash)
  check('INACTIVE persisted in metadata',meta.is_active is False and edited.data['status']=='INACTIVE')
  check('Updated Date advanced and Created Date stayed unchanged',row.created_at==initial_created and meta.updated_at>=initial_updated)
  new_password='LiveVerifyChanged#2026'; changed=api('PATCH',{'kind':'delivery','id':did,'password':new_password,'confirm_password':new_password,'status':'ACTIVE'})
  row.refresh_from_db(); meta.refresh_from_db(); check('new password rehashed and verifies',changed.status_code==200 and row.password.startswith('$argon2id$') and verify_php_password(new_password,row.password))
  check('ACTIVE reactivation persisted',meta.is_active is True and changed.data['status']=='ACTIVE')
  search=api('GET',query='kind=delivery&search='+username+'&page=1&page_size=5'); check('search finds test account',search.status_code==200 and any(x['id']==did for x in search.data['results']))
  page=api('GET',query='kind=delivery&page=2&page_size=1'); check('pagination parameters honored',page.status_code==200 and page.data['page']==2 and page.data['page_size']==1)
  anon=api('GET',query='kind=delivery',auth=False); check('anonymous access denied',anon.status_code in (401,403))
  denied=api('GET',query='kind=delivery',role='DELIVERY_BOY'); check('Delivery Boy role denied Admin listing',denied.status_code==403)
  allowed=api('GET',query='kind=delivery',role='ADMIN'); check('ADMIN role is allowed to list Delivery Boys',allowed.status_code==200)
  try:
   with transaction.atomic():
    with connection.cursor() as cursor:
     cursor.execute('INSERT INTO warehouse_delivery_boys (warehouse_id, delivery_boy_id, created_at) VALUES (%s, %s, NOW())', [1, did])
    blocked=api('DELETE',query='kind=delivery&id='+str(did))
    check('warehouse-assigned Delivery Boy deletion blocked with HTTP 409',blocked.status_code==409)
    raise FixtureRollback()
  except FixtureRollback: pass
  try:
   with transaction.atomic():
    from django.utils import timezone
    from legacydb.models import Checkout
    order=Checkout.objects.create(user='rollback-verification',full_name='Verification Driver',address='Test Address',city='Kochi',state='Kerala',postal_code='682001',country='India',phone='9876543210',email='verification@example.invalid',total_amount='1.00',order_date=timezone.now(),status='Pending',delivery_boy_id=did,view='',payment_method='Test',shipping_charge='0')
    blocked=api('DELETE',query='kind=delivery&id='+str(did))
    check('order-history Delivery Boy deletion blocked with HTTP 409',blocked.status_code==409)
    check('order-history block advises deactivation','Inactive' in blocked.data.get('detail',''))
    raise FixtureRollback()
  except FixtureRollback: pass
  raise Rollback()
except Rollback: pass
after=list(DeliveryBoy.objects.order_by('id').values_list('id',flat=True)); check('test rows rolled back and existing IDs preserved',after==before and DeliveryBoy.objects.filter(pk=1).exists())
print('RESULT:',len(checks),'/',len(checks),'passed; 0 failed')
print('All DB writes rolled back; PostgreSQL sequences may advance.')