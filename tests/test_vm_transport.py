import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from vm_transport import prepare, VmTestSession
from mncs_test import AdapterError

WS=ROOT.parent;COMPILER=WS/'mncs-compiler';VM=WS/'mncs-vm/target/debug/mncs-vm'
@unittest.skipUnless(VM.is_file() and (COMPILER/'.bootstrap/target/release/mncs-compiler-stage0-probe').is_file(),'selected providers not built')
class TestCallableMembrane(unittest.TestCase):
    def test_identity_reference_is_enforced_by_vm_and_transport(self):
        with tempfile.TemporaryDirectory() as raw:
            product,evidence,inventory=prepare(checkout=COMPILER,executable=None,source=ROOT/'tests/self_suite.mncs',libraries=[WS/'mncs-stdlib/library',ROOT/'native'],cache=Path(raw),timeout=120)
            test=next(t for t in inventory['tests'] if t.get('status')!='skip')
            binding=next(b for b in evidence['callable_bindings'] if b['callable_identity']==test['function_identity'])
            ref={k:binding[k] for k in ('callable_identity','declaration_identity','test_case_identity','signature_identity')};ref['artifact_identity']=product['artifact']['identity']
            session=VmTestSession(vm_checkout=WS/'mncs-vm',vm_executable=VM,product=product,evidence=evidence,timeout=120,adapter_error=AdapterError)
            request={'schema_version':'0.1','target':{'module':binding['module'],'function':binding['function']},'arguments':[],'step_budget':100000}
            try:
                accepted=session.runtime.call(request,callable_reference=ref);self.assertEqual(accepted['execution']['invoked_callable'],ref)
                forged={**ref,'signature_identity':'sha256:'+'0'*64}
                with self.assertRaisesRegex(RuntimeError,'identity reference mismatch'):session.runtime.call(request,callable_reference=forged)
                valid=session.runtime.call(request,callable_reference=ref);self.assertEqual(valid['record']['returned'],accepted['record']['returned'])
            finally:session.close()
