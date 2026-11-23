import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from triage_engine import ENGINE_VERSION, run
from triage_engine.llm import _runner, run_llm
from triage_engine.pipeline.semantic_contract import passage, validate

ENV = {"LLM_API_KEY": "test", "LLM_BASE_URL": "https://unused.invalid"}


def rows():
    return [
        {"src": "news-spider", "date": "2026-11-20", "title": "光伏项目预计明年投运",
         "body": "安装工作已完成，预计明年商业运营。", "url": "https://example.org/a"},
        {"src": "literature", "date": "2026-11-20", "title": "光伏器件研究",
         "body": "引言" * 650 + "实验证实光伏器件的性能变化。", "doi": "10.1234/abc"},
    ]


def decision(req):
    evidence = {"field": "title", "quote": req["title"]}
    dec = {"i": req["i"], "domain_path": "光伏", "domain_no": "D01",
            "type_id": "T01" if req["src"] == "literature" else "T08", "terms": ["光伏"],
            "alternatives": [], "reason": "根据题名识别对象与主事件", "confidence": "high",
            "event": req["title"], "event_state": "原文未说明" if req["src"] == "literature" else "计划中",
            "domain_evidence": evidence, "event_evidence": evidence}
    if dec["type_id"] == "T08":
        dec["event"] = "投运"
        dec["stage_facts"] = [dict(evidence, action="投运", state=dec["event_state"])]
    return dec


def test_prompt_and_constraints():
    system, domains, types = _runner.build_contract()
    assert ENGINE_VERSION in system
    assert _runner.TYPES[0]["definition"] in system
    assert _runner.TYPES[0]["boundaries"][0][1] in system
    req = dict(passage(rows()[1]), i=1)
    good = decision(req)
    assert not validate(good, req, domains, types)
    for change in ({"domain_path": "自拟"}, {"domain_no": "D02"}, {"type_id": "T25"},
                   {"type_id": "T09"}, {"type_id": []}, {"event_state": "猜测"},
                   {"event_evidence": {"field": "body", "quote": req["title"]}},
                   {"domain_evidence": None}, {"confidence": []}, {"alternatives": [None]}):
        assert validate(dict(good, **change), req, domains, types), change


def test_retry_expanded_evidence_resume_and_merge():
    calls = []

    def provider(env, system, message, timeout):
        requests = json.loads(message)
        calls.append(requests)
        answers = []
        for req in requests:
            answer = decision(req)
            if req["src"] == "literature":
                if "validation_error" not in req:
                    answers.append({"i": req["i"], "status": "insufficient_evidence"})
                    continue
                assert "实验证实" in req["body"]
                answer["event_evidence"] = {"field": "body", "quote": "实验证实光伏器件的性能变化。"}
            answers.append(answer)
        return json.dumps(answers)

    with TemporaryDirectory() as out, patch.object(_runner, "call_llm", side_effect=provider):
        records = rows()
        # Legacy positional audit cannot satisfy the new contract.
        Path(out, "llm_semantic_audit.jsonl").write_text('{"decisions":[{"i":0}]}\n')
        result = run_llm(records, out, env=ENV, sleep=0)
        assert result["accepted"] == 2
        assert [[r["i"] for r in req] for req in calls] == [[0, 1], [1]]
        merged = run(records, out, llm_decisions=result)
        assert all(x["llmJudged"] for x in merged["items"])
        paper = next(x for x in merged["items"] if x["src"] == "literature")
        assert paper["llmEvidence"]["event_evidence"]["field"] == "body"
        assert run_llm(records[::-1], out, env=ENV, sleep=0)["decisions"] == result["decisions"]
        assert len(calls) == 2
        records[0]["body"] += "新增进度。"
        assert run(records, out, llm_decisions=result)["stats"]["llmJudged"] == 1
        run_llm(records[::-1], out, env=ENV, sleep=0)
        assert len(calls) == 3 and calls[-1][0]["title"] == records[0]["title"]


def test_invalid_reply_falls_back_even_with_force_all():
    records = rows()
    calls = []

    def provider(env, system, message, timeout):
        reqs = json.loads(message)
        calls.append([r["i"] for r in reqs])
        if len(reqs) > 1:
            valid = decision(reqs[0])
            return json.dumps([valid, valid, dict(valid, i=10000)])
        return "invalid json" if reqs[0]["i"] == 0 else json.dumps([dict(decision(reqs[0]), type_id="T23")])

    with TemporaryDirectory() as out, patch.object(_runner, "call_llm", side_effect=provider):
        result = run_llm(records, out, env=ENV, force_all=True, sleep=0)
        assert calls == [[0, 1], [0], [1]]
        assert result["requested"] == result["fallback_count"] == 2
        assert result["judged"] == result["accepted"] == 0
        assert run(records, out, llm_decisions=result) == run(records, out)
        audit = [json.loads(line) for line in Path(out, "llm_semantic_audit.jsonl").read_text().splitlines()]
        assert [r["error"] for r in audit[:2]] == ["同一编号返回多次", "缺少本条结果"]
        assert audit[0]["unexpected_in_batch"] == 1


def test_confidence_filter_and_changed_model():
    calls = []

    def provider(env, system, message, timeout):
        calls.append(env.get("LLM_MODEL"))
        return json.dumps([dict(decision(r), confidence="medium") for r in json.loads(message)])

    with TemporaryDirectory() as out, patch.object(_runner, "call_llm", side_effect=provider):
        records = rows()[:1]
        assert run_llm(records, out, env=ENV, sleep=0)["skipped_low_conf"] == 1
        assert run_llm(records, out, env=ENV, sleep=0, force_all=True)["accepted"] == 1
        run_llm(records, out, env=dict(ENV, LLM_MODEL="other"), sleep=0)
        assert len(calls) == 2


def test_stage_fact_checks_with_authentic_quotes():
    _, domains, types = _runner.build_contract()
    shown = dict(passage(rows()[0]), i=0)
    shown["body"] = "已完成设备安装，预计明年商业运营。"
    installed = {"field": "body", "quote": "已完成设备安装", "action": "设备安装", "state": "已发生"}
    planned = {"field": "body", "quote": "预计明年商业运营", "action": "商业运营", "state": "计划中"}

    def judgment(selected, facts):
        return dict(decision(shown), event=selected["action"], event_state=selected["state"],
                    event_evidence={k: selected[k] for k in ("quote", "field")}, stage_facts=facts)

    for main in (installed, planned):
        assert not _runner.validate_decision(judgment(main, [installed, planned]), shown, domains, types)
    assert _runner.validate_decision(dict(judgment(planned, [installed, planned]), event_state="已发生"),
                                     shown, domains, types)
    for quote in ("商业运营", "预计明年商业运营"):
        wrong = dict(planned, quote=quote, state="已发生")
        assert "S-A05" in _runner.validate_decision(judgment(wrong, [installed, wrong]), shown, domains, types)
    for text, action in (("项目计划开工", "开工"), ("项目尚未投运", "投运")):
        shown["body"] = text
        wrong = dict(installed, quote=action, action=action, state="已发生")
        assert _runner.validate_decision(judgment(wrong, [wrong]), shown, domains, types)
    shown["body"] = "一期已正式投运，二期预计明年投运。"
    first = dict(installed, quote="一期已正式投运", action="投运")
    second = dict(planned, quote="二期预计明年投运", action="投运")
    assert not _runner.validate_decision(judgment(first, [first, second]), shown, domains, types)


def test_carrier_checks_with_correct_and_inverted_types():
    from triage_engine.tests.test_smoke import _rows

    _, domains, types = _runner.build_contract()
    cases = [
        (_rows()[10], "论文报道", "T23", "T01"),
        (_rows()[11], "论文本体", "T01", "T23"),
        (_rows()[8], "政策解读", "T23", "T19"),
        (dict(rows()[0], title="国家发展改革委发布《光伏产业专项行动方案》"), "政策发布", "T19", "T23"),
    ]
    for row, kind, right, wrong in cases:
        shown = dict(passage(row), i=0)
        quote = {"field": "title", "quote": shown["title"]}
        dec = dict(decision(shown), type_id=right, terms=[], event=shown["title"],
                   event_evidence=quote, carrier_fact=dict(quote, kind=kind))
        dec.pop("stage_facts")
        assert not _runner.validate_decision(dec, shown, domains, types), right
        # Both label and carrier are inverted; the original quote remains authentic.
        wrong_kind = {"政策解读": "政策发布", "政策发布": "政策解读",
                      "论文本体": "论文报道", "论文报道": "论文本体"}[kind]
        bad = dict(dec, type_id=wrong, carrier_fact=dict(quote, kind=wrong_kind))
        assert _runner.validate_decision(bad, shown, domains, types), wrong


def test_fact_conflict_retries_falls_back_and_is_checked_on_replay():
    requests = []

    def provider(env, system, message, timeout):
        req = json.loads(message)[0]
        requests.append(req)
        dec = decision(req)
        dec["stage_facts"][0]["state"] = dec["event_state"] = "已发生"
        return json.dumps([dec])

    with TemporaryDirectory() as out, patch.object(_runner, "call_llm", side_effect=provider):
        records = rows()[:1]
        result = run_llm(records, out, env=ENV, sleep=0, force_all=True)
        assert "S-A05" in requests[1]["validation_error"]
        assert len(requests) == 2 and result["fallback_count"] == 1
        assert run(records, out, llm_decisions=result) == run(records, out)
    with TemporaryDirectory() as out, patch.object(
            _runner, "call_llm", side_effect=lambda e, s, m, t: json.dumps([decision(json.loads(m)[0])])):
        result = run_llm(records, out, env=ENV, sleep=0)
        dec = result["decisions"][records[0]["url"]]
        item = run(records, out, llm_decisions=result)["items"][0]
        assert item["llmEvidence"]["stage_facts"] == dec["stage_facts"]
        assert not run(records, out, llm_decisions=dict(result, version="evidence-v1"))["items"][0]["llmJudged"]
        dec["stage_facts"][0]["state"] = dec["event_state"] = "已发生"
        assert not run(records, out, llm_decisions=result)["items"][0]["llmJudged"]
