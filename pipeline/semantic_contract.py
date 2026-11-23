"""Shared input, vocabulary and evidence checks for versioned model decisions."""
import hashlib
import json

ENGINE_VERSION = "1.17"
CONTRACT_VERSION = "evidence-v1"
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
    return {"src": item["src"], "title": item["title"] if expanded else item["title"][:160],
            "body": body[:RETRY_BODY if expanded else INITIAL_BODY]}


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
    if dec.get("event_state") not in ("计划中", "进行中", "已发生", "原文未说明", "不适用"):
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
        ev = dec.get(key)
        if not isinstance(ev, dict) or ev.get("field") not in ("title", "body"):
            return key + " 须注明 title 或 body"
        quote = ev.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in shown[ev["field"]]:
            return key + " 引文未在指定输入字段逐字匹配"
    return ""
