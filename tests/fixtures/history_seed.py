"""
Seeds two CommitSnapshot nodes with known PropertyVersion records for testing
the history and diff operations.

Commit A (older): PaymentService.charge signature = "(amount: Decimal) -> ChargeResult"
Commit B (newer): PaymentService.charge signature = "(amount: Decimal, currency: str) -> ChargeResult"
                  Also: new BazelTarget //services/subscriptions:sub_lib added
                        DEPENDS_ON edge from //services/checkout to //services/subscriptions added
"""
import pytest
from datetime import datetime, timezone, timedelta
from axon.db.connection import sync_session

SHA_A = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
SHA_B = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
TS_A = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
TS_B = datetime(2025, 1, 2, 10, 0, 0, tzinfo=timezone.utc)

SYM_ID = "symbol::services/payment/src/payment.py::PaymentService.charge"
SIG_A = "(amount: Decimal) -> ChargeResult"
SIG_B = "(amount: Decimal, currency: str) -> ChargeResult"

SEED_HISTORY = f"""
// Base Graph (Missing from seed_graph)
MERGE (chk:BazelTarget {{label: '//services/checkout:checkout_lib'}})
SET chk.id = 'target::::services/checkout:checkout_lib',
    chk.kind = 'PY_LIBRARY',
    chk.package = '//services/checkout',
    chk.name = 'checkout_lib';

MERGE (pay:BazelTarget {{label: '//services/payment:payment_lib'}})
SET pay.id = 'target::::services/payment:payment_lib',
    pay.kind = 'PY_LIBRARY',
    pay.package = '//services/payment',
    pay.name = 'payment_lib';

// CommitSnapshot A
MERGE (csa:CommitSnapshot {{sha: '{SHA_A}'}})
SET csa.commit_timestamp = datetime('{TS_A.isoformat()}'),
    csa.author = 'engineer-a@company.com',
    csa.branch = 'main',
    csa.graph_version_id = 'v-a',
    csa.index_success = true,
    csa.changed_targets = ['//services/payment:payment_lib'],
    csa.breaking_api_changes = 0;

// CommitSnapshot B
MERGE (csb:CommitSnapshot {{sha: '{SHA_B}'}})
SET csb.commit_timestamp = datetime('{TS_B.isoformat()}'),
    csb.author = 'engineer-b@company.com',
    csb.branch = 'main',
    csb.graph_version_id = 'v-b',
    csb.index_success = true,
    csb.changed_targets = ['//services/payment:payment_lib',
                           '//services/subscriptions:sub_lib'],
    csb.breaking_api_changes = 1;

// Symbol exists (live graph)
MERGE (sym:Symbol {{id: '{SYM_ID}'}})
SET sym.symbol_id = 'services/payment/src/payment.py::PaymentService.charge',
    sym.name = 'charge',
    sym.qualified_name = 'payment.PaymentService.charge',
    sym.kind = 'METHOD',
    sym.language = 'PYTHON',
    sym.file_path = 'services/payment/src/payment.py',
    sym.line_start = 34,
    sym.line_end = 68,
    sym.is_public = true,
    sym.signature = '{SIG_B}';

// PropertyVersion: signature changed from A to B
MERGE (pv:PropertyVersion {{id: 'propver::test-sig-change'}})
SET pv.node_id = '{SYM_ID}',
    pv.node_label = 'Symbol',
    pv.property_name = 'signature',
    pv.old_value = '{SIG_A}',
    pv.new_value = '{SIG_B}',
    pv.changed_at_commit = '{SHA_B}',
    pv.changed_at = datetime('{TS_B.isoformat()}'),
    pv.author = 'engineer-b@company.com',
    pv.is_breaking = true;

MATCH (pv:PropertyVersion {{id: 'propver::test-sig-change'}})
MATCH (csb:CommitSnapshot {{sha: '{SHA_B}'}})
MERGE (pv)-[:CHANGED_IN]->(csb);

// New target added in commit B
MERGE (sub:BazelTarget {{id: 'target::::services/subscriptions:sub_lib'}})
SET sub.label = '//services/subscriptions:sub_lib',
    sub.kind = 'PY_LIBRARY',
    sub.package = '//services/subscriptions',
    sub.name = 'sub_lib',
    sub.build_file_path = 'services/subscriptions/BUILD',
    sub.is_genrule = false, sub.testonly = false;

MATCH (sub:BazelTarget {{label: '//services/subscriptions:sub_lib'}})
MATCH (csb:CommitSnapshot {{sha: '{SHA_B}'}})
MERGE (sub)-[:INTRODUCED_BY]->(csb);

// DEPENDS_ON edge added in commit B
MATCH (chk:BazelTarget {{label: '//services/checkout:checkout_lib'}})
MATCH (sub:BazelTarget {{label: '//services/subscriptions:sub_lib'}})
MERGE (chk)-[r:DEPENDS_ON {{dep_type: 'DIRECT'}}]->(sub)
SET r.declared_in = 'services/checkout/BUILD',
    r.valid_from_commit = '{SHA_B}',
    r.valid_until_commit = null;
"""


@pytest.fixture(scope="session")
def history_seed():
    """Adds history-specific data."""
    with sync_session() as session:
        for stmt in SEED_HISTORY.split(";"):
            stmt = stmt.strip()
            if stmt:
                session.run(stmt)
    yield
    with sync_session() as session:
        session.run("""
            MATCH (n) WHERE n.id STARTS WITH 'propver::'
                OR (n:CommitDaySummary)
            DETACH DELETE n
        """)
        session.run(f"""
            MATCH (cs:CommitSnapshot)
            WHERE cs.sha IN ['{SHA_A}', '{SHA_B}']
            DETACH DELETE cs
        """)