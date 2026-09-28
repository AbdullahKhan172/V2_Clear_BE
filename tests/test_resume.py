"""
Resume and ownership: does a refresh lose anything, and are runs isolated?
==========================================================================
Two guarantees, and the first is the one users notice:

  RESUME     everything entered in steps 1-4 comes back from a COLD store -
             no client state, no cache, nothing but the run id. If this passes,
             a refresh cannot lose work, because the browser was never holding
             it.

  OWNERSHIP  one owner cannot read, write, list or delete another's runs, and
             a legacy run (owner NULL) is adopted rather than orphaned.

Plus the specific bug this replaced: `max_step` must NOT fall when the user
walks back. It is what the step rail enables, so a falling high-water mark
re-locks every step ahead and strands them at Step 2.

Run:  .venv/Scripts/python.exe webapp/backend/tests/test_resume.py
"""

import io
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parents[1]

_TMP = Path(tempfile.mkdtemp(prefix='clear_resume_'))
# Against Postgres when CLEAR_TEST_DATABASE_URL is set, throwaway SQLite
# otherwise. The suite must pass on BOTH: SQLite is what a developer runs,
# Postgres is what is deployed, and they differ in ways that matter
# (types, transaction behaviour, JSON handling).
os.environ['DATABASE_URL'] = (os.environ.get('CLEAR_TEST_DATABASE_URL')
                              or f'sqlite:///{(_TMP / "resume.db").as_posix()}')
# Local disk, always — unless run_suite.py is explicitly pointing the
# whole suite at object storage. Without this a developer's .env, which
# may hold REAL production credentials, would silently make every
# harness write test junk into a live bucket.
os.environ.setdefault('STORAGE_BACKEND', 'local')
os.environ['BLOB_DIR'] = str(_TMP / 'blobs')

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

BOQ = b"""Category,Material,Description,Volume(m3),Count,Level
Slab/Floor,Concrete,L1 flat slab,100,1,Level 1
Column,Concrete,Column 400x400,12,8,Level 1
Wall,Concrete,Core wall 250,45,4,Level 2
"""

ALICE = {'X-Client-Id': 'alice-browser'}
BOB = {'X-Client-Id': 'bob-browser'}


def _upload(c, headers, name='Resume Test'):
    r = c.post('/api/uploads',
               files={'file': ('boq.csv', io.BytesIO(BOQ), 'text/csv')},
               data={'project_name': name, 'area': '2500',
                     'stage': 'Detailed Design',
                     'structural_system': 'RC Flat Slab',
                     'location': 'Dublin', 'client': 'ACME'},
               headers=headers)
    assert r.status_code == 200, r.text
    run_id = r.json()['run_id']
    for _ in range(200):
        s = c.get(f'/api/runs/{run_id}/status', headers=headers).json()
        if s['status'] in ('parsed', 'failed'):
            break
        time.sleep(0.15)
    assert s['status'] == 'parsed', s
    return run_id


def main() -> int:
    problems: list[str] = []

    def check(ok: bool, label: str, detail: str = '') -> None:
        print(f'  {"+" if ok else "x"} {label}' + (f' — {detail}' if detail else ''))
        if not ok:
            problems.append(f'{label}: {detail}')

    with TestClient(app) as c:
        run_id = _upload(c, ALICE)

        # ── 1. Fill in every step, as the wizard does ───────────────────
        c.put(f'/api/runs/{run_id}/selection', headers=ALICE, json={
            'excluded_categories': ['Wall'],
            'excluded_levels': ['Level 2'],
            'excluded_groups': []})
        c.put(f'/api/runs/{run_id}/materials', headers=ALICE, json={
            'defaults': {'concrete_grade': '40/50 MPa', 'ggbs_pct': 50},
            'rows': {'Column|||Column 400x400':
                     {'grade': '32/40 MPa', 'ggbs': 25, 'rebarRate': 325}},
            'epd_overrides': {}})
        c.put(f'/api/runs/{run_id}/transport', headers=ALICE, json={
            'distances': {'in_situ': {'sea': 0, 'road': 250}},
            'waste_pct': {'Concrete': 12.0},
            'a5a_factor': 41.5})
        c.put(f'/api/runs/{run_id}/progress', headers=ALICE, json={'step': 5})
        print(f'  saved four steps against run {run_id[:8]}\n')

        # ── 2. Read it all back from a COLD store ───────────────────────
        # A brand-new client: no cookies, no cache, nothing but the run id.
        # This is what a refresh actually is.
        with TestClient(app) as cold:
            sel = cold.get(f'/api/runs/{run_id}/groups',
                           headers=ALICE).json()['selection']
            check(sel['excluded_categories'] == ['Wall']
                  and sel['excluded_levels'] == ['Level 2'],
                  'Step 2 selection survives', str(sel))

            mats = cold.get(f'/api/runs/{run_id}/materials',
                            headers=ALICE).json()['materials']
            row = (mats['rows'] or {}).get('Column|||Column 400x400', {})
            check(mats['defaults'].get('concrete_grade') == '40/50 MPa'
                  and mats['defaults'].get('ggbs_pct') == 50
                  and row.get('ggbs') == 25 and row.get('rebarRate') == 325,
                  'Step 3 materials survive',
                  f"defaults={mats['defaults']} row={row}")

            tr = cold.get(f'/api/runs/{run_id}/transport',
                          headers=ALICE).json()['transport']
            check(tr['distances'].get('in_situ', {}).get('road') == 250
                  and tr['waste_pct'].get('Concrete') == 12.0
                  and tr['a5a_factor'] == 41.5,
                  'Step 4 transport survives', str(tr))

            prog = cold.get(f'/api/runs/{run_id}/progress', headers=ALICE).json()
            check(prog['max_step'] == 5,
                  'the furthest step reached survives', str(prog))

            # Step 1's own details, which the client needs to refill the form.
            els = cold.get(f'/api/runs/{run_id}/elements', headers=ALICE).json()
            p = els.get('project') or {}
            check(p.get('name') == 'Resume Test' and float(p.get('area')) == 2500
                  and p.get('stage') == 'Detailed Design'
                  and p.get('location') == 'Dublin',
                  'Step 1 project details survive', str(p))

        # ── 3. The mark only ever rises ─────────────────────────────────
        # The client no longer reports the current step at all, but a stale tab
        # or a replayed request still must not be able to LOWER the mark: that
        # would re-lock every step ahead and strand the user.
        back = c.put(f'/api/runs/{run_id}/progress',
                     headers=ALICE, json={'step': 2}).json()
        check(back['max_step'] == 5,
              'a lower step cannot pull the mark down', str(back))

        forward = c.put(f'/api/runs/{run_id}/progress',
                        headers=ALICE, json={'step': 6}).json()
        check(forward['max_step'] == 6, 'a higher step raises it', str(forward))

        # A step out of range would arrive from a URL, so it is coerced.
        for raw, want in ((99, 6), (-3, 6), (0, 6)):
            got = c.put(f'/api/runs/{run_id}/progress',
                        headers=ALICE, json={'step': raw}).json()['max_step']
            check(got == want, f'step {raw} clamps, mark stays {want}', str(got))

        # ── 3b. A run with NO recorded progress must not open locked ────
        # Every run created before progress was tracked has an empty
        # config['wizard']. Trusting that literally reports "Step 1 of 6" and
        # opens the wizard with Steps 2-6 disabled - and Step 1 cannot be
        # completed again, because a browser cannot put a File back into a file
        # input. The run's own lifecycle is the harder evidence.
        from app import store as _store
        raw = _store.get(run_id)
        cfg = dict(raw.config or {})
        cfg.pop('wizard', None)                     # as if never recorded
        _store.update(run_id, config=cfg)

        recovered = c.get(f'/api/runs/{run_id}/progress', headers=ALICE).json()
        check(recovered['max_step'] >= 2,
              'a parsed run with no recorded progress still opens past Step 1',
              f"max_step={recovered['max_step']}")

        listed = next(r for r in c.get('/api/runs', headers=ALICE).json()['runs']
                      if r['run_id'] == run_id)
        check(listed['max_step'] >= 2,
              'and the projects list agrees', f"max_step={listed['max_step']}")

        # A CALCULATED run has been through all six.
        _store.update(run_id, status='complete',
                      calculated_at=__import__('datetime').datetime.now(
                          __import__('datetime').timezone.utc))
        done = c.get(f'/api/runs/{run_id}/progress', headers=ALICE).json()
        check(done['max_step'] == 6,
              'a calculated run unlocks every step', f"max_step={done['max_step']}")

        # Restore, so the checks below see the run as they expect.
        _store.update(run_id, status='parsed', calculated_at=None)
        c.put(f'/api/runs/{run_id}/progress', headers=ALICE, json={'step': 5})
        still = c.get(f'/api/runs/{run_id}/progress', headers=ALICE).json()

        # ── 4. Ownership isolation ──────────────────────────────────────
        print()
        before_bob = c.get(f'/api/runs/{run_id}/progress', headers=ALICE).json()
        for label, resp in (
            ('read', c.get(f'/api/runs/{run_id}/groups', headers=BOB)),
            ('progress', c.get(f'/api/runs/{run_id}/progress', headers=BOB)),
            ('results', c.get(f'/api/runs/{run_id}/dashboard', headers=BOB)),
            ('write', c.put(f'/api/runs/{run_id}/progress', headers=BOB,
                            json={'step': 1})),
            ('delete', c.delete(f'/api/runs/{run_id}', headers=BOB)),
        ):
            check(resp.status_code == 404,
                  f'another browser cannot {label} this run',
                  f'got {resp.status_code}, expected 404')

        # 404 rather than 403 on purpose: confirming a run EXISTS is itself a
        # disclosure.
        alice_runs = c.get('/api/runs', headers=ALICE).json()['runs']
        bob_runs = c.get('/api/runs', headers=BOB).json()['runs']
        check(any(r['run_id'] == run_id for r in alice_runs),
              'the owner sees the run in their list')
        check(all(r['run_id'] != run_id for r in bob_runs),
              'another browser does not', f'{len(bob_runs)} rows')

        # Bob's write attempt must not have moved Alice's progress.
        still = c.get(f'/api/runs/{run_id}/progress', headers=ALICE).json()
        check(still['max_step'] == before_bob['max_step'],
              'a refused write changed nothing', str(still))

        # ── 5. The listing carries what the screen shows ────────────────
        row = next(r for r in alice_runs if r['run_id'] == run_id)
        # Compared against the live progress rather than a literal: the clamp
        # checks above legitimately pushed the high-water mark to 6, and
        # max_step only ever grows.
        check(row['name'] == 'Resume Test' and row['status'] == 'parsed'
              and row['max_step'] == still['max_step'],
              'the listing agrees with the recorded progress',
              f"listing={row['max_step']} progress={still['max_step']}")

        # ── 6. A legacy run is adopted, not orphaned ────────────────────
        print()
        from app import store
        legacy = store.create(filename='old.csv', ext='.csv', upload_key='',
                              project={'name': 'Pre-ownership'}, owner_id=None)
        check(store.get(legacy.id).owner_id is None, 'a legacy run starts unowned')
        # READING must not claim it: opening a link someone shared should not
        # quietly take their run away.
        read = c.get(f'/api/runs/{legacy.id}/progress', headers=BOB)
        check(read.status_code == 200,
              'any browser can still open it', f'got {read.status_code}')
        check(store.get(legacy.id).owner_id is None,
              'and reading it does NOT claim it',
              str(store.get(legacy.id).owner_id))
        check(c.get(f'/api/runs/{legacy.id}/progress',
                    headers=ALICE).status_code == 200,
              'so it is still readable by others too')

        # WRITING claims it - the person editing it is the one with a claim.
        c.put(f'/api/runs/{legacy.id}/progress', headers=BOB, json={'step': 2})
        check(store.get(legacy.id).owner_id == 'bob-browser',
              'but the first browser to CHANGE it becomes the owner',
              str(store.get(legacy.id).owner_id))
        check(c.get(f'/api/runs/{legacy.id}/progress',
                    headers=ALICE).status_code == 404,
              'and it is no longer visible to everyone else')

        # ── 7. Sign-in migration: a browser's runs follow the person ────
        moved = store.claim_runs('bob-browser', 'user:42')
        check(moved >= 1, 'signing in claims this browser\'s runs', f'{moved} moved')
        check(c.get(f'/api/runs/{legacy.id}/progress',
                    headers={'X-Client-Id': 'user:42'}).status_code == 200,
              'the signed-in user now owns them')
        check(c.get(f'/api/runs/{legacy.id}/progress',
                    headers=BOB).status_code == 404,
              'and the anonymous browser no longer does')

    if problems:
        print(f'\nRESUME CHECK FAILED ({len(problems)})')
        for p in problems[:20]:
            print('   ', p)
        return 1

    print('\nRESUME PASSED - every step survives a cold reload, progress never '
          'goes backwards, and runs are isolated per owner')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
