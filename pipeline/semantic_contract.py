"""Shared input, vocabulary and evidence checks for versioned model decisions."""
import hashlib
import json
import re

ENGINE_VERSION = "1.17"
CONTRACT_VERSION = "evidence-v2"
EVENT_STATES = ("计划中", "进行中", "已发生", "原文未说明", "不适用")
INITIAL_BODY = 1100
RETRY_BODY = 12000


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def record_id(item):
    fields = ("src", "date", "url", "doi", "title", "body")
    return "row:" + digest([item.get(k) or "" for k in fields])


def passage(item, expanded=False):
    body = item.get("body") or ""
    shown = {"src": item["src"], "title": item["title"] if expanded else item["title"][:160],
             "body": body[:RETRY_BODY if expanded else INITIAL_BODY]}
    # Only expose the DOI indicator needed by the carrier rule, not the entire source URL.
    if item.get("doi") or re.search(r"\b10\.\d{4,9}/\S+|arXiv[:\s]*\d+|\bDOI\b|doi\.org",
                                    item.get("url") or "", re.I):
        shown["doi"] = item.get("doi") or "链接含 DOI/arXiv 线索"
    return shown


def domain_menu(domains, extensions, leaves):
    menu = {d["name"]: d["no"] for d in domains}
    menu.update({d["path"]: d["no"] for d in extensions})
    registered = set(menu.values())
    for leaf in leaves:
        menu[leaf["path"]] = leaf["target"] if leaf["target"] in registered else ""
    return menu


def validate(dec, shown, domains, types):
    """Return the first actionable error, or an empty string for a valid judgment."""
    if "status" in dec and dec["status"] != "ok":
        return "判定状态无效或证据不足"
    path, no, tid = (dec.get(k) for k in ("domain_path", "domain_no", "type_id"))
    if not isinstance(path, str) or path not in domains or domains[path] != no:
        return "领域路径和编号必须为菜单中的同一对值"
    if not isinstance(tid, str) or tid not in types or tid == "T25":
        return "请选择当前菜单内的实型"
    if shown["src"] == "literature" and tid != "T01":
        return "文献源的类型须为 T01"
    if dec.get("confidence") not in ("high", "medium", "low"):
        return "confidence 须为 high、medium 或 low"
    for key in ("reason", "event"):
        if not isinstance(dec.get(key), str) or not dec[key].strip():
            return key + " 须为非空文本"
    if dec.get("event_state") not in EVENT_STATES:
        return "请填写主事件的 event_state"
    for key, maximum in (("terms", 4), ("alternatives", 3)):
        values = dec.get(key)
        if not isinstance(values, list) or len(values) > maximum:
            return key + " 须为限长列表"
        for value in values:
            if not isinstance(value, str) or not value.strip():
                return key + " 含无效值"
            if key == "alternatives" and (value not in types or value == "T25"):
                return "备选类型须为菜单内实型"
            if key == "terms" and not any(value in shown[f] for f in ("title", "body")):
                return "terms 应摘自当前标题或正文"
    for key in ("domain_evidence", "event_evidence"):
        error = evidence_error(dec.get(key), shown)
        if error:
            return key + " " + error
    return ""


def evidence_error(evidence, shown):
    if not isinstance(evidence, dict) or evidence.get("field") not in ("title", "body"):
        return "须注明 title 或 body"
    quote = evidence.get("quote")
    if not isinstance(quote, str) or not quote.strip() or quote not in shown[evidence["field"]]:
        return "引文未在指定输入字段逐字匹配"
    return ""


def validate_stages(dec, shown, future_landed):
    facts = dec.get("stage_facts", [])
    if not isinstance(facts, list):
        return "stage_facts 须为动作事实列表"
    if not facts and dec["type_id"] not in ("T08", "T10"):
        return ""
    selected = False
    for fact in facts:
        error = evidence_error(fact, shown)
        if error:
            return "stage_facts " + error
        action, state = fact.get("action"), fact.get("state")
        quote = fact["quote"]
        if not isinstance(action, str) or not action.strip() or action not in quote:
            return "动作须逐字摘自该条事实的引文"
        if state not in EVENT_STATES:
            return "请标明每个项目动作自己的状态"
        if quote.count(action) != 1:
            return "事实引文中动作出现多次，请缩小引文以区分不同阶段"
        source = shown[fact["field"]]
        # Resolve the verb at its source position, including qualifiers omitted from the quote.
        # Bind the existing S-A05 pattern to its terminal verb, not another verb in the sentence.
        for occurrence in re.finditer(re.escape(quote), source):
            end = occurrence.start() + quote.index(action) + len(action)
            start = end - len(action)
            # Include just enough surrounding source to catch both a clipped qualifier
            # and a clipped verb. S-A05's longest prefix spans fewer than 40 characters.
            left = max(0, start - 40)
            for marker in future_landed.finditer(source, left, min(len(source), end + 8)):
                verb_start, verb_end = marker.span("action")
                if start >= verb_end or end <= verb_start:
                    continue
                if not (occurrence.start() <= marker.start() and occurrence.end() >= marker.end()
                        and state == "计划中"):
                    return "S-A05：动作「" + action + "」须保留原文「" + marker.group() + "」并标为计划中"
            # Direct qualifiers for construction/start and explicitly negated actions.
            # These checks concern the quoted fact only; they never set a score or band.
            before = source[max(0, start - 8):start]
            if re.search(r"(?:拟|计划|即将|预计)$", before) and state == "已发生":
                return "动作「" + action + "」前有计划限定，不能据此认定已发生"
            if re.search(r"(?:尚未|暂未|还未|并未|未能)$", before) and state == "已发生":
                return "动作「" + action + "」前有否定限定，不能据此认定已发生"
        if (action == dec["event"] and state == dec["event_state"]
                and all(fact[k] == dec["event_evidence"][k] for k in ("field", "quote"))):
            selected = True
    if not selected:
        return "主事件的动作、状态和引文须对应同一条 stage_facts"
    return ""
