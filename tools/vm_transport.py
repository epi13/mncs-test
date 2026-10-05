"""Canonical VM transport for the existing native TestResult/suite authority.

Selection is explicit; an unavailable, stale or unsupported VM lane never
falls back to another compiler/runtime. Stage-0 stays an independent lane.
"""
from __future__ import annotations
import importlib.util
import json
import sys
import time
from pathlib import Path


def compiler_provider(checkout):
    path = Path(checkout).resolve() / 'tools/vm_provider.py'
    spec = importlib.util.spec_from_file_location('mncs_selected_compiler_provider', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(*, checkout, executable, source, libraries, cache, timeout):
    provider_module = compiler_provider(checkout)
    provider = provider_module.CompilerProvider(Path(checkout), executable=Path(executable) if executable else None, timeout=timeout)
    request = {'schema_version':'mncs.compiler-vm-request/1', 'source':str(Path(source).resolve()),
               'logical_name':Path(source).name, 'libraries':[str(Path(p).resolve()) for p in libraries], 'include_tests':True}
    product = provider.emit(request, Path(cache))
    evidence = json.loads((Path(product['cache']) / product['evidence']['address']).read_text())
    inventory = evidence['test_inventory']
    if not isinstance(inventory, dict):
        raise RuntimeError('canonical compiler omitted first-class test inventory')
    bindings = {r['callable_identity']:r for r in evidence['callable_bindings']}
    for test in inventory['tests']:
        binding = bindings[test['function_identity']]
        test['signature_identity'] = binding['signature_identity']
    return product, evidence, inventory


class VmTestSession:
    def __init__(self, *, vm_checkout, vm_executable, product, evidence, timeout, adapter_error):
        path = Path(vm_checkout).resolve() / 'python/mncs_vm_client/__init__.py'
        spec = importlib.util.spec_from_file_location('test_selected_vm_transport', path)
        client = importlib.util.module_from_spec(spec); spec.loader.exec_module(client)
        Session = client.Session
        self.runtime = Session(Path(vm_executable), Path(product['cache']) / product['artifact']['address'], timeout=timeout, build_receipt=product['build_receipt'])
        self.product, self.evidence = product, evidence
        self.adapter_error = adapter_error
        self.timings = []
        self.bindings = {r['callable_identity']:r for r in evidence['callable_bindings']}

    def info(self):
        return {'artifact_identity':self.runtime.info['artifact_id'], 'artifact_sha256':self.runtime.artifact_sha256,
                'runtime':self.runtime.runtime, 'producer':self.product['producer']}

    def call_batch(self, requests):
        started = time.perf_counter()
        results = []
        try:
            for raw in requests:
                if raw.get('grants'):
                    raise RuntimeError('canonical Test VM lane has no host-grant bindings')
                ref = raw.get('callable_reference')
                if ref:
                    if ref['artifact_identity'] != self.runtime.info['artifact_id']:
                        raise RuntimeError('test callable artifact mismatch')
                    binding = self.bindings[ref['callable_identity']]
                    for field in ('declaration_identity','test_case_identity','signature_identity'):
                        if binding[field] != ref[field]:
                            raise RuntimeError('test callable compiler binding mismatch: ' + field)
                    module, function = binding['module'], binding['function']
                else:
                    module, function = raw['module'], raw['function']
                request = {'schema_version':'0.1', 'target':{'module':module,'function':function},
                           'arguments':raw['args'], 'step_budget':raw['step_budget']}
                if 'type_arguments' in raw: request['type_arguments'] = raw['type_arguments']
                result = self.runtime.call(request, callable_reference=ref)
                result['execution']['vm_record'] = result['record']
                result['execution']['execution_provenance'] = result['execution_provenance']
                result['execution']['reused_session'] = self.runtime.count > 1
                results.append(result['execution'])
        except (OSError, KeyError, ValueError, RuntimeError) as error:
            raise self.adapter_error(str(error)) from error
        self.timings.append({'phase':'canonical_vm_batch', 'calls':len(requests), 'wall_time_ms':round((time.perf_counter()-started)*1000,3)})
        return results

    def close(self):
        self.runtime.close()
