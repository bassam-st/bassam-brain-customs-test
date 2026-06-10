import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "customs-backup-2026-06-09.json"
EXCHANGE_RATE_CONFIG_FILE = BASE_DIR / "data" / "exchange_rate_config.json"

app = FastAPI(title="Bassam Brain Customs Test")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

SESSIONS: Dict[str, Dict[str, Any]] = {}
ARABIC_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652]")

def load_exchange_rate_config() -> Dict[str, Any]:
    if not EXCHANGE_RATE_CONFIG_FILE.exists():
        return {"exchange_rate": 1563.0, "admin_pin": "bassam1234"}
    try:
        with open(EXCHANGE_RATE_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"exchange_rate": 1563.0, "admin_pin": "bassam1234"}

def save_exchange_rate_config(config: Dict[str, Any]):
    EXCHANGE_RATE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EXCHANGE_RATE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

def get_current_exchange_rate() -> float:
    config = load_exchange_rate_config()
    return float(config.get("exchange_rate", 1563.0))

def load_backup() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {"items": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def base_items() -> List[Dict[str, Any]]:
    data = load_backup()
    return [item for item in data.get("items", []) if item.get("status", "active") == "active"]

def normalize_ar(text: Any) -> str:
    text = str(text or "").strip().lower()
    text = ARABIC_DIACRITICS.sub("", text)
    replacements = {
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه",
        "گ": "ك", "چ": "ج", "پ": "ب",
    }
    for a, b in replacements.items():
        text = text.replace(a, b)
    text = re.sub(r"[^\w\s\u0600-\u06FF./-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def item_text(item: Dict[str, Any]) -> str:
    fields = [
        item.get("name"), item.get("normalizedName"), item.get("hsCode"),
        item.get("priceUnit"), item.get("categoryLabel"),
        item.get("shortDescription"), item.get("longDescription"), item.get("notes"),
    ]
    fields.extend(item.get("aliases") or [])
    fields.extend(item.get("searchTokens") or [])
    return normalize_ar(" ".join(str(x or "") for x in fields))

def score_item(query: str, item: Dict[str, Any]) -> int:
    q = normalize_ar(query)
    if not q:
        return 0
    hay = item_text(item)
    score = 0
    if q in hay:
        score += 80
    q_words = [w for w in q.split() if len(w) > 1]
    for w in q_words:
        if w in hay:
            score += 12
    for alias in item.get("aliases") or []:
        alias_n = normalize_ar(alias)
        if alias_n and alias_n in q:
            score += 30
    if normalize_ar(item.get("name")) in q:
        score += 60
    hs = str(item.get("hsCode") or "")
    if hs and hs in q:
        score += 100
    return score

def search_items(query: str, items: Optional[List[Dict[str, Any]]] = None, limit: int = 8) -> List[Dict[str, Any]]:
    items = items or base_items()
    scored = []
    for item in items:
        s = score_item(query, item)
        if s > 0:
            scored.append((s, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:limit]]

def extract_numbers(text: str) -> List[float]:
    text = normalize_ar(text).replace(",", ".")
    matches = re.findall(r"\d+(?:\.\d+)?", text)
    return [float(m) for m in matches]

def classify_price_unit(price_unit: str) -> str:
    u = normalize_ar(price_unit)
    if "امبير" in u or "ملي امبير" in u:
        return "ampere"
    if "كيلو وات" in u or "كيلوواط" in u or "kw" in u:
        return "kw"
    if "الوات" in u or "وات" in u:
        return "watt"
    if "طن" in u:
        return "ton"
    if "كيلو" in u or "كجم" in u or "كغم" in u:
        return "kg"
    if "لتر" in u:
        return "liter"
    if "كرتون" in u:
        return "carton"
    if "زوجه" in u or "زوج" in u:
        return "pair_pack"
    return "piece"

def unit_question(item: Dict[str, Any]) -> str:
    unit_type = classify_price_unit(item.get("priceUnit", ""))
    unit = item.get("priceUnit", "الوحدة")
    if unit_type == "ampere":
        return f"الصنف سعره حسب {unit}. كم العدد؟ وكم أمبير كل واحدة؟ مثال: 10 بطاريات 100 أمبير."
    if unit_type == "watt":
        return f"الصنف سعره حسب {unit}. اكتب إجمالي الواط أو عدد الألواح × واط اللوح. مثال: 10 ألواح 550 وات."
    if unit_type == "kw":
        return f"الصنف سعره حسب {unit}. كم كيلو وات؟ مثال: 5 كيلو وات."
    if unit_type == "kg":
        return f"الصنف سعره حسب {unit}. كم الوزن بالكيلو؟"
    if unit_type == "ton":
        return f"الصنف سعره حسب {unit}. كم الكمية بالطن؟"
    if unit_type == "liter":
        return f"الصنف سعره حسب {unit}. كم عدد اللترات؟"
    if unit_type == "carton":
        return f"الصنف سعره حسب {unit}. كم عدد الكراتين؟"
    return f"الصنف سعره حسب {unit}. كم الكمية؟"

def compute_units(item: Dict[str, Any], text: str) -> Tuple[Optional[float], str, Optional[str]]:
    nums = extract_numbers(text)
    unit_type = classify_price_unit(item.get("priceUnit", ""))
    if not nums:
        return None, unit_question(item), None
    ntext = normalize_ar(text)

    if unit_type == "ampere":
        if len(nums) >= 2:
            return nums[0] * nums[1], f"إجمالي الأمبير = {nums[0]:g} × {nums[1]:g} = {nums[0] * nums[1]:g} أمبير", None
        if "امبير" in ntext:
            return nums[0], f"إجمالي الأمبير = {nums[0]:g} أمبير", None
        return None, "كم أمبير كل بطارية؟ اكتب مثلًا: 100 أمبير لكل واحدة، أو اكتب الإجمالي بالأمبير.", None

    if unit_type == "watt":
        if len(nums) >= 2 and ("لوح" in ntext or "الواح" in ntext):
            return nums[0] * nums[1], f"إجمالي الواط = {nums[0]:g} × {nums[1]:g} = {nums[0] * nums[1]:g} وات", None
        return nums[0], f"إجمالي الواط = {nums[0]:g} وات", None

    if unit_type == "kw":
        return nums[0], f"إجمالي الكيلو وات = {nums[0]:g} كيلو وات", None
    if unit_type == "ton":
        return nums[0], f"الكمية = {nums[0]:g} طن", None
    if unit_type == "kg":
        return nums[0], f"الكمية = {nums[0]:g} كيلو", None
    if unit_type == "liter":
        return nums[0], f"الكمية = {nums[0]:g} لتر", None
    if unit_type == "carton":
        return nums[0], f"الكمية = {nums[0]:g} كرتون", None
    # New logic for handling cartons/parcels/boxes with internal units
    package_words = ["كرتون", "كرتونين", "كراتين", "طرد", "طرود", "صندوق", "صناديق"]
    if any(word in ntext for word in package_words):
        package_count = 0
        package_word = ""
        for word in package_words:
            match = re.search(r"(\d+)\s*" + word, ntext)
            if match:
                package_count = float(match.group(1))
                package_word = word
                break
        
        if package_count > 0:
            # Check for internal units like dozen, pair, piece
            internal_unit_match = re.search(r"(\d+)\s*(درزن|دزينة|زوج|أزواج|حبة|قطعة)", ntext)
            if internal_unit_match:
                internal_quantity = float(internal_unit_match.group(1))
                internal_unit_type = internal_unit_match.group(2)
                
                # Convert to base units based on item's priceUnit
                item_price_unit_normalized = normalize_ar(item.get("priceUnit", ""))
                
                calculated_units = 0
                calculation_note = ""
                
                if "درزن" in item_price_unit_normalized or "دزينة" in item_price_unit_normalized or "12 زوج" in item_price_unit_normalized:
                    if "درزن" in internal_unit_type or "دزينة" in internal_unit_type:
                        calculated_units = package_count * internal_quantity
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} درزن"
                    elif "زوج" in internal_unit_type or "أزواج" in internal_unit_type:
                        calculated_units = package_count * internal_quantity / 12  # 1 dozen pairs = 12 pairs
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} درزن"
                    elif "حبة" in internal_unit_type or "قطعة" in internal_unit_type:
                        calculated_units = package_count * internal_quantity / 24 # 1 dozen pairs = 24 pieces
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} درزن"
                
                elif "زوج" in item_price_unit_normalized or "أزواج" in item_price_unit_normalized:
                    if "درزن" in internal_unit_type or "دزينة" in internal_unit_type:
                        calculated_units = package_count * internal_quantity * 12 # 1 dozen pairs = 12 pairs
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} زوج"
                    elif "زوج" in internal_unit_type or "أزواج" in internal_unit_type:
                        calculated_units = package_count * internal_quantity
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} زوج"
                    elif "حبة" in internal_unit_type or "قطعة" in internal_unit_type:
                        calculated_units = package_count * internal_quantity / 2 # 1 pair = 2 pieces
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} زوج"
                
                elif "حبة" in item_price_unit_normalized or "قطعة" in item_price_unit_normalized:
                    if "درزن" in internal_unit_type or "دزينة" in internal_unit_type:
                        calculated_units = package_count * internal_quantity * 12 # 1 dozen = 12 pieces
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} حبة"
                    elif "زوج" in internal_unit_type or "أزواج" in internal_unit_type:
                        calculated_units = package_count * internal_quantity * 2 # 1 pair = 2 pieces
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} حبة"
                    elif "حبة" in internal_unit_type or "قطعة" in internal_unit_type:
                        calculated_units = package_count * internal_quantity
                        calculation_note = f"{package_count:g} {package_word} × {internal_quantity:g} {internal_unit_type} = {calculated_units:g} حبة"
                
                if calculated_units > 0:
                    return calculated_units, f"الكمية المحسوبة: {calculated_units:g} وحدة من {item.get('priceUnit', '')}", calculation_note
            
            # If package count is given but internal units are not, ask for clarification
            item_price_unit_normalized = normalize_ar(item.get("priceUnit", ""))
            if any(u in item_price_unit_normalized for u in ["درزن", "دزينة", "زوج", "أزواج", "حبة", "قطعة", "12 زوج"]):
                return None, f"كم داخل كل {package_word}؟ هل العدد بالدرزن أم بالزوج أم بالحبة؟", None

    # Existing logic for simple quantities
    return nums[0], f"الكمية = {nums[0]:g}", None

def result_text(item: Dict[str, Any], units: float, units_note: str, original_query_text: str = "", calculation_details: Optional[str] = None) -> str:
    price = float(item.get("usdPrice") or 0)
    factor = float(item.get("categoryFactor") or 0)
    rate = get_current_exchange_rate()
    total = price * units * rate * factor
    
    lines = [
        f"الصنف: {item.get('name')}",
        f"البند الجمركي: {item.get('hsCode') or 'غير محدد'}",
        f"السعر المعتمد: {price:g} دولار لكل {item.get('priceUnit') or 'وحدة'}"
    ]
    if original_query_text:
        lines.append(f"الطلب الأصلي: {original_query_text}")
    lines.extend([
        f"طريقة الفهم: {calculation_details}",
        f"{units_note}",
        f"الفئة: {item.get('categoryLabel') or ''}",
        f"المعامل: {factor:g}",
        f"سعر الصرف: {rate:g} ريال لكل دولار",
        "",
        "الحساب:",
        f"{price:g} × {units:g} × {rate:g} × {factor:g} = {total:,.0f} ريال يمني",
        "",
        f"النتيجة: جمارك {item.get('name')} = {total:,.0f} ريال يمني تقريبًا."
    ])
    return "\n".join(lines)

def format_matches(matches: List[Dict[str, Any]]) -> str:
    lines = ["وجدت أكثر من صنف قريب. اختر الرقم المقصود:"]
    for i, item in enumerate(matches, 1):
        lines.append(
            f"{i}. {item.get('name')} — السعر: {item.get('usdPrice')} دولار / {item.get('priceUnit')} — الفئة: {item.get('categoryLabel')} — البند: {item.get('hsCode') or 'غير محدد'}"
        )
    return "\n".join(lines)

def get_session(session_id: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    sid = session_id or str(uuid.uuid4())
    if sid not in SESSIONS:
        SESSIONS[sid] = {"created_at": time.time(), "mode": "customs"}
    return sid, SESSIONS[sid]

async def google_search_serper(query: str) -> str:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": 5},
            )
            r.raise_for_status()
            data = r.json()
            parts = []
            for item in data.get("organic", [])[:5]:
                parts.append(f"- {item.get('title')}: {item.get('snippet')} ({item.get('link')})")
            return "\n".join(parts)
    except Exception:
        return ""

async def call_groq(prompt: str, search_context: str = "") -> Optional[str]:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    messages = [
        {"role": "system", "content": "أنت مساعد عربي مفيد. أجب بدقة وباختصار واضح. إذا توفر سياق بحث فاستخدمه ولا تخترع."},
        {"role": "user", "content": (f"سياق بحث:\n{search_context}\n\n" if search_context else "") + prompt},
    ]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.2},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"تعذر الاتصال بـ Groq حاليًا: {e}"

async def call_anthropic(prompt: str, search_context: str = "") -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": 900,
                    "temperature": 0.2,
                    "system": "أنت مساعد عربي مفيد. أجب بدقة وباختصار واضح.",
                    "messages": [{"role": "user", "content": (f"سياق بحث:\n{search_context}\n\n" if search_context else "") + prompt}],
                },
            )
            r.raise_for_status()
            data = r.json()
            return "\n".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    except Exception as e:
        return f"تعذر الاتصال بـ Anthropic حاليًا: {e}"

async def parse_customs_query_with_llm(query: str) -> Dict[str, Any]:
    prompt = f"""حلل استعلام الجمارك التالي واستخرج منه المعلومات المطلوبة في صيغة JSON. إذا كانت المعلومة غير موجودة، اترك الحقل فارغًا أو null. لا تخترع معلومات.
الاستعلام: {query}
الناتج بصيغة JSON فقط:
{{
  "item_query": "اسم السلعة",
  "quantity": null,
  "unit": "الوحدة",
  "package_count": null,
  "units_per_package": null,
  "capacity": null,
  "capacity_unit": "الوحدة",
  "missing_fields": []
}}"""
    response = await call_groq(prompt)
    if not response or "تعذر الاتصال" in response:
        response = await call_anthropic(prompt)
    
    if response:
        try:
            json_match = re.search(r"(\{.*\})", response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            return json.loads(response)
        except Exception:
            return {}
    return {}

async def customs_chat(message: str, session_id: Optional[str], extra_items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    sid, session = get_session(session_id)
    items = base_items() + (extra_items or [])
    msg = message.strip()

    if session.get("step") == "select_item":
        nums = extract_numbers(msg)
        matches = session.get("matches") or []
        if nums and 1 <= int(nums[0]) <= len(matches):
            item = matches[int(nums[0]) - 1]
            session.update({"step": "need_quantity", "selected_item": item})
            return {"session_id": sid, "reply": f"تم اختيار: {item.get('name')}\n{unit_question(item)}", "matches": []}
        new_matches = search_items(msg, matches, limit=3)
        if len(new_matches) == 1:
            item = new_matches[0]
            session.update({"step": "need_quantity", "selected_item": item})
            return {"session_id": sid, "reply": f"تم اختيار: {item.get('name')}\n{unit_question(item)}", "matches": []}
        return {"session_id": sid, "reply": "اكتب رقم الصنف من القائمة، مثل: 1 أو 2.", "matches": matches}

    if session.get("step") == "need_quantity" and session.get("selected_item"):
        item = session["selected_item"]
        units, note, calculation_details = compute_units(item, msg)
        if units is None:
            session["original_query_text"] = msg # Store original query for later use
            return {"session_id": sid, "reply": note, "matches": []}
        original_query_text = session.pop("original_query_text", "")
        session.clear()
        session.update({"created_at": time.time(), "mode": "customs"})
        return {"session_id": sid, "reply": result_text(item, units, note, original_query_text, calculation_details), "matches": []}

    parsed = await parse_customs_query_with_llm(msg)
    session["original_query_text"] = msg # Store original query for later use
    item_query = parsed.get("item_query") or msg
    matches = search_items(item_query, items)
    
    if not matches:
        return {"session_id": sid, "reply": "لم أجد هذا الصنف. حاول كتابة اسم الصنف بوضوح.", "matches": []}

    if len(matches) == 1 or (len(matches) > 1 and score_item(item_query, matches[0]) >= score_item(item_query, matches[1]) + 45):
        item = matches[0]
        units, note, calculation_details = compute_units(item, msg)
        if units is not None:
            return {"session_id": sid, "reply": result_text(item, units, note, msg, calculation_details), "matches": []}
        session.update({"step": "need_quantity", "selected_item": item})
        return {"session_id": sid, "reply": f"وجدت الصنف: {item.get('name')}\n{unit_question(item)}", "matches": [item]}

    session.update({"step": "select_item", "matches": matches})
    return {"session_id": sid, "reply": format_matches(matches), "matches": matches}

async def general_chat(message: str, use_search: bool = True) -> str:
    search_context = await google_search_serper(message) if use_search else ""
    reply = await call_groq(message, search_context)
    if reply and "تعذر الاتصال" not in reply:
        return reply
    reply = await call_anthropic(message, search_context)
    if reply and "تعذر الاتصال" not in reply:
        return reply
    if search_context:
        return "نتائج البحث المتاحة:\n" + search_context
    return "الوضع العام يحتاج مفاتيح API. وضع الجمارك يعمل بدونها."

@app.post("/api/admin/verify_pin")
async def verify_admin_pin(payload: Dict[str, str] = Body(...)) -> JSONResponse:
    config = load_exchange_rate_config()
    if payload.get("pin") == config.get("admin_pin"):
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "message": "رمز PIN غير صحيح"}, status_code=401)

@app.get("/api/admin/exchange_rate")
async def get_admin_exchange_rate() -> JSONResponse:
    config = load_exchange_rate_config()
    return JSONResponse({"exchange_rate": config.get("exchange_rate", 1563.0)})

@app.post("/api/admin/exchange_rate")
async def update_admin_exchange_rate(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    config = load_exchange_rate_config()
    if payload.get("pin") != config.get("admin_pin"):
        return JSONResponse({"success": False, "message": "رمز PIN غير صحيح"}, status_code=401)
    try:
        new_rate = float(payload.get("exchange_rate"))
        if new_rate <= 0:
            raise ValueError("سعر الصرف يجب أن يكون رقمًا موجبًا")
        config["exchange_rate"] = new_rate
        save_exchange_rate_config(config)
        return JSONResponse({"success": True, "exchange_rate": new_rate})
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)

@app.get("/api/health")
async def health_check() -> JSONResponse:
    return JSONResponse({"status": "ok", "items": len(base_items()), "exchange_rate": get_current_exchange_rate()})

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")

@app.get("/api/customs/items")
def api_items(q: str = ""):
    if q:
        return {"items": search_items(q, base_items(), limit=20)}
    return {"items": base_items()}

@app.post("/api/chat")
async def api_chat(payload: Dict[str, Any] = Body(...)):
    message = str(payload.get("message") or "")
    mode = payload.get("mode") or "customs"
    session_id = payload.get("session_id")
    extra_items = payload.get("extra_items") or []
    
    if mode == "customs":
        return await customs_chat(message, session_id, extra_items=extra_items)
    
    reply = await general_chat(message, use_search=bool(payload.get("use_search", True)))
    return {"session_id": session_id or str(uuid.uuid4()), "reply": reply, "matches": []}
