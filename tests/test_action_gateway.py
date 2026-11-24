import asyncio
from concurrent.futures import ThreadPoolExecutor
import pytest
from cloakup.storage import Database
from cloakup.action_gateway import ActionLedger,ActionBlocked,CapabilityRegistry,CapabilityAdapter
from cloakup.tools import ToolOutcome


def test_atomic_resource_lease_and_unknown_survives_new_run(tmp_path):
 db=Database(tmp_path);ledger=ActionLedger(db)
 def claim(i):
  try:return ledger.claim(task_id='task',resource='record',capability='write',identity={'value':i},run_id='run')
  except ActionBlocked:return None
 with ThreadPoolExecutor(max_workers=2) as pool:claims=list(pool.map(claim,[1,2]))
 ids=[i for i in claims if i];assert len(ids)==1
 ledger.transition(ids[0],'executing');ledger.recover('run')
 restored=ActionLedger(Database(tmp_path));assert restored.get(ids[0])['state']=='unknown'
 with pytest.raises(ActionBlocked):restored.claim(task_id='task',resource='record',capability='write',identity={'value':'different'},run_id='new-run')
 restored.transition(ids[0],'verified',evidence='real receipt')
 with pytest.raises(ActionBlocked):restored.claim(task_id='task',resource='record',capability='write',identity={'value':1 if claims[0] else 2})


def test_known_not_executed_may_retry_but_cannot_fake_success(tmp_path):
 ledger=ActionLedger(Database(tmp_path));kw={'task_id':'t','resource':'r','capability':'save','identity':'same'}
 ident=ledger.claim(**kw);ledger.transition(ident,'executing')
 with pytest.raises(ValueError):ledger.transition(ident,'verified')
 ledger.transition(ident,'failed',reason='authorization rejected before execution')
 assert ledger.claim(**kw)==ident


async def test_api_preferred_and_failed_write_never_falls_back():
 registry=CapabilityRegistry();calls=[]
 async def api(*a):calls.append('api');raise RuntimeError('unknown write')
 async def browser(*a):calls.append('browser');return ToolOutcome('ok')
 registry.register('save',CapabilityAdapter('browser','browser',lambda c:True,browser))
 registry.register('save',CapabilityAdapter('official-api','api',lambda c:True,api))
 with pytest.raises(RuntimeError):await registry.run('save',{}, {'authorized':True})
 assert calls==['api']
 with pytest.raises(ActionBlocked):await registry.run('save',{}, {'authorized':False})


async def test_unavailable_api_uses_browser_and_receipt_verifier():
 registry=CapabilityRegistry()
 async def execute(*a):return ToolOutcome('submitted')
 async def verify(*a):return {'verified':True,'evidence':'record id 123'}
 registry.register('save',CapabilityAdapter('api','api',lambda c:False,execute))
 registry.register('save',CapabilityAdapter('browser','browser',lambda c:True,execute,verify))
 outcome=await registry.run('save',{}, {'authorized':True})
 assert outcome.detail['receipt_verified'] and outcome.detail['transport']=='browser'


def test_claimed_but_not_executing_can_recover_as_known_failure(tmp_path):
 ledger=ActionLedger(Database(tmp_path));kw={'task_id':'t','resource':'r','capability':'save','identity':'same','run_id':'old'}
 ident=ledger.claim(**kw);ledger.recover('old')
 assert ledger.get(ident)['state']=='failed' and ledger.claim(**kw)==ident


async def test_registry_rechecks_authority_after_availability():
 registry=CapabilityRegistry();state={'allowed':True};calls=[]
 def check():
  if not state['allowed']:raise ActionBlocked('paused')
 def available(context):state['allowed']=False;return True
 async def execute(*a):calls.append(1);return ToolOutcome('bad')
 registry.register('save',CapabilityAdapter('api','api',available,execute))
 with pytest.raises(ActionBlocked):await registry.run('save',{}, {'authorized':True,'assert_authorized':check})
 assert not calls

def test_native_unknown_remains_locked_but_new_answer_can_send(tmp_path):
 ledger=ActionLedger(Database(tmp_path));kw={'task_id':'case','resource':'candidate:p','capability':'ask_resume','identity':'one'}
 native=ledger.claim(**kw);ledger.transition(native,'executing');ledger.transition(native,'unknown')
 ledger.isolate_platform_requests();ledger.isolate_platform_requests()
 assert ledger.get(native)['state']=='unknown' and ledger.get(native)['resource']=='candidate:p:platform-request'
 with pytest.raises(ActionBlocked):ledger.claim(**{**kw,'resource':'candidate:p:platform-request'})
 answer=ledger.claim(task_id='case',resource='candidate:p',capability='send_message',identity='new-answer')
 assert ledger.get(answer)['state']=='claimed'
