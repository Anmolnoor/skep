"""ES-F5 (edge sweep B34): a verdict names the layer its rule came from.

``decide()`` labelled every rule with ``base.template or operator.template``,
so a rule from the approved baseline read ``operator-default/net-fetch-gated``
— the operator document's name on the baseline's rule (I8).
"""

from __future__ import annotations

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor import policy_baseline as pb
from skep.supervisor.policy_resolver import resolve_operator_policy
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.policy_templates import load_policy_template
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import predating_home


def test_a_baseline_rule_is_labelled_by_the_baseline(config: SupervisorConfig) -> None:
    store = RunStore(predating_home(config.db_path))
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        pb.write_lock(config.home, load_policy_template("builder"), approved_by="op")
        decision = resolve_operator_policy(store).decision("network", "fetch", "example.com")
        assert decision.rule_id == "net-fetch-gated"
        assert decision.decided_by == "template:builder/net-fetch-gated"
        record = policy_decide(
            ConfigHolder(config, store), store, scope="network", action="fetch", value="x.org"
        )
        assert record["decided_by"] == "template:builder/net-fetch-gated"
        # A rule from the operator document keeps the document's label.
        search = resolve_operator_policy(store).decision("network", "search", "ddgs")
        assert search.decided_by == "operator-default/net:search"
    finally:
        store.close()
