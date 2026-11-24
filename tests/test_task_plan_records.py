from cloakup.workbench import Workbench


def test_plan_records_load_each_task_once_and_keep_stale_flags(tmp_path,monkeypatch):
 m=Workbench(tmp_path)
 try:
  loads=[];definitions=[]
  def definition(key):
   definitions.append(key)
   if key=='missing':raise KeyError(key)
   return {'id':key,'config':{'case_ids':['c1','c2']}}
  monkeypatch.setattr(m.planning,'definition',definition)
  monkeypatch.setattr(m.cases,'load',lambda cid:loads.append(cid) or {'id':cid})
  monkeypatch.setattr('cloakup.task_planning.planning_signature',lambda d,cases:'sig-'+d['id']+'-'+str(len(cases)))
  for i,(task,sig) in enumerate([('t1','sig-t1-2'),('t1','old'),('t1','sig-t1-2'),('t2','sig-t2-2'),('missing','x')]):
   m.db.put('plan','p%d'%i,{'id':'p%d'%i,'task_id':task,'signature':sig})
  stale={r['id']:r['stale'] for r in m.task_plan_records()}
  assert stale=={'p0':False,'p1':True,'p2':False,'p3':False,'p4':True}
  # 同一任务的多条历史计划只加载一次岗位文档。
  assert sorted(loads)==['c1','c1','c2','c2']
  assert definitions.count('t1')==1 and definitions.count('t2')==1
 finally:m.close()
