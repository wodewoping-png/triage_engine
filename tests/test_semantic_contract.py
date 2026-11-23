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
    return {"i": req["i"], "domain_path": "光伏", "domain_no": "D01",
            "type_id": "T01" if req["src"] == "literature" else "T08", "terms": ["光伏"],
            "alternatives": [], "reason": "根据题名识别对象与主事件", "confidence": "high",
            "event": req["title"], "event_state": "原文未说明" if req["src"] == "literature" else "计划中",
            "domain_evidence": evidence, "event_evidence": evidence}


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
