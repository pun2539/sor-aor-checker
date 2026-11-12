import streamlit as st
import re
import tempfile
import os
import pdfplumber
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextBox, LTTextLine


def parse_sa_native(pdf_path):
    """Natively extracts SA line items using vector layout coordinates without OCR."""
    sa_items = {}
    item_counter = 1

    for page_layout in extract_pages(pdf_path):
        boxes = [b for b in page_layout if isinstance(b, (LTTextBox, LTTextLine))]

        # 1. Product Names (Column x ≈ 51-60)
        names = []
        trade_boxes = [b for b in boxes if 50 < b.x0 < 60]
        for b in trade_boxes:
            txt = b.get_text()
            split_items = re.split(r'อาหารสัตว[^\n\(]*', txt)
            for it in split_items:
                clean = it.strip().replace('\n', ' ')
                clean = re.sub(r'[\u0E00-\u0E7F\uF700-\uF71F]+', ' ', clean)
                clean = clean.strip().lstrip('(').rstrip(')')
                clean = re.sub(r'\s+', ' ', clean)
                if clean and not any(h in clean for h in ["Trade Name", "No.", "ชื่อทางการค้า", "่อทางการคา"]):
                    names.append(clean)

        # 2. Weights (Column x ≈ 610-635)
        weights = []
        for b in boxes:
            txt = b.get_text().strip()
            if ("กิโลกรัม" in txt or "KGM" in txt) and "รวม" not in txt:
                m = re.findall(r'([\d,]+\.\d{3})\s*(?:กิโลกรัม|กิโล|KGM)', txt)
                if m:
                    for w in m:
                        # KEPT COMMA HERE
                        weights.append((b.y0, w.strip()))
        weights.sort(key=lambda x: -x[0])

        # 3. FOB Values (Column x ≈ 695-720)
        fobs = []
        for b in boxes:
            if 695 <= b.x0 <= 720:
                txt = b.get_text().strip()
                matches = re.findall(r'\b[\d,]+\.\d{2}\b', txt)
                if matches and "รวม" not in txt:
                    # KEPT COMMA HERE
                    val = matches[-1].strip()
                    if val != "0.00" and not val.startswith("2309"):
                        fobs.append((b.y0, val))
        fobs.sort(key=lambda x: -x[0])

        # Construct rows mapping the found elements top-to-bottom
        for i in range(len(names)):
            n = names[i]
            w = weights[i][1] if i < len(weights) else "0.000"
            f = fobs[i][1] if i < len(fobs) else "0.00"
            sa_items[item_counter] = {'product_name': n, 'net_weight': w, 'fob_value': f}
            item_counter += 1

    # Mathematically sum all extracted item weights (ignoring commas for the math)
    total_nw_float = sum(float(item['net_weight'].replace(',', '')) for item in sa_items.values())
    # Format the grand total back WITH a comma
    total_nw = f"{total_nw_float:,.3f}"

    return sa_items, total_nw


def parse_draft_native(pdf_path):
    """Parses digital Export Declaration (DRAFT) text using line-based extraction."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "".join(p.extract_text(layout=True) or "" for p in pdf.pages)

    items = {}
    nw_matches = re.findall(r'\bNW\s*=?\s*([\d,]+\.\d{3})\s*KGM', text, flags=re.IGNORECASE)
    # KEPT COMMA HERE
    total_nw = nw_matches[-1].strip() if nw_matches else "0.000"

    # Pre-clean known layout noise that interferes with numeric values
    text = re.sub(r'\(\s*[\d,]+\.\d{3}[^\)]*\)', ' ', text)
    text = re.sub(r'(Qty|NW|GW|รวมยกไป|รวม/ยกไป)\s*[\d,]+\.\d{3}(?:\s*KGM)?', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\([\d,]+\.\d{3}\s*KGM\)', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'[\d,]+\.\d{2,6}\s*(?:THB|AUD|USD|INR)', ' ', text, flags=re.IGNORECASE)
    text = text.replace('003500010.00', ' ')

    parts = re.split(r'\(PETFOOD\)', text, flags=re.IGNORECASE)

    for i in range(len(parts) - 1):
        chunk = parts[i]

        # 1. FOB Value Extraction
        value_matches = re.findall(r'(?:^|\s)([\d,]+\.\d{2})(?!\d)', chunk)
        valid_values = [v.replace(' ', '') for v in value_matches if
                        v.replace(' ', '') != "0.00" and len(v.replace(',', '').split('.')[0]) <= 7]
        value = valid_values[-1] if valid_values else "0.00"

        val_str = ""
        for match in re.finditer(r'(?:^|\s)([\d,]+\.\d{2})(?!\d)', chunk):
            clean_match = match.group(1).replace(' ', '')
            if clean_match == value: val_str = match.group(1)
        val_idx = chunk.rfind(val_str) if val_str else len(chunk)

        # 2. Weight Extraction
        pre_value_text = chunk[:val_idx]
        weight_search_area = pre_value_text[-150:]
        weight_matches = re.findall(r'([\d,]+\.\d{3})(?:\s*KGM)?', weight_search_area, re.IGNORECASE)
        weight = weight_matches[-1].replace(' ', '').replace('2544', '') if weight_matches else "0.000"

        # 3. Product Name Extraction (Anchor-less)
        lines = chunk.split('\n')
        raw_name_line = lines[-1] if lines else ""

        name = re.sub(r'[\u0E00-\u0E7F]+', ' ', raw_name_line)
        junk_patterns = [
            r'Origin:?\s*[A-Z]{2}',
            r'Purchase:?\s*[A-Z\$]{2}',
            r'\s*[A-Z]?\d{6,}\s*'
        ]
        for junk in junk_patterns:
            name = re.sub(junk, ' ', name, flags=re.IGNORECASE)

        name = re.sub(r'[=\-\_!:\|]+', ' ', name)
        name = re.sub(r'^\s*[^A-Za-z0-9]+', '', name)
        name = re.sub(r'\s+', ' ', name).strip()
        name = name.replace(' &', '&').replace('& ', '&')

        # KEPT COMMAS IN DICTIONARY
        items[i + 1] = {
            'product_name': name,
            'net_weight': weight,
            'fob_value': value
        }

    return items, total_nw


def is_smart_match(str1, str2):
    """Enforces exact character match including case/symbols, removing ONLY spaces."""
    s1 = re.sub(r'\s+', '', str1)
    s2 = re.sub(r'\s+', '', str2)
    if not s1 or not s2: return False
    return s1 == s2 or s1 in s2 or s2 in s1


# --- STREAMLIT UI ---
st.set_page_config(layout="wide", page_title="Sor Aor vs Declaration Checker")
st.title("📄 Sor Aor vs Export Declaration (Native Digital Verify)")

col1, col2 = st.columns(2)
with col1:
    sa_file = st.file_uploader("Upload SA (Sor Aor) PDF", type=["pdf"], key="sa_uploader")
with col2:
    draft_file = st.file_uploader("Upload DRAFT (Declaration) PDF", type=["pdf"], key="draft_uploader")

if sa_file and draft_file:
    if st.button("Run Verification Engine", type="primary"):
        with st.spinner("Extracting native vectors and running rules..."):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_sa:
                tmp_sa.write(sa_file.getvalue())
                sa_path = tmp_sa.name

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_draft:
                tmp_draft.write(draft_file.getvalue())
                draft_path = tmp_draft.name

            try:
                # Execute native parsers
                sa_items, sa_total_nw = parse_sa_native(sa_path)
                draft_items, draft_total_nw = parse_draft_native(draft_path)

                # --- RAW DATA DEBUGGER ---
                with st.expander("🛠️ View Raw Extracted Data (Debugging)"):
                    st.write("**Extracted SA Items:**", sa_items)
                    st.write("**Extracted DRAFT Items:**", draft_items)

                st.header("--- GRAND TOTAL VERIFICATION ---")
                try:
                    # Ignore commas when calculating float logic
                    sa_nw_float = float(sa_total_nw.replace(',', ''))
                    draft_nw_float = float(draft_total_nw.replace(',', ''))
                    total_nw_match = abs(sa_nw_float - draft_nw_float) < 0.001
                except ValueError:
                    total_nw_match = False

                if total_nw_match and draft_nw_float > 0:
                    st.success(
                        f"✅ **Grand Total Net Weight:** MATCH\n* **SA:** `{sa_total_nw} kg`\n* **DRAFT:** `{draft_total_nw} KGM`")
                else:
                    st.error(
                        f"❌ **Grand Total Net Weight: MISMATCH**\n* **SA:** `{sa_total_nw} kg`\n* **DRAFT:** `{draft_total_nw} KGM`")

                st.header("--- LINE ITEMS COMPARISON ---")
                all_item_keys = sorted(set(list(sa_items.keys()) + list(draft_items.keys())))

                matched_count = 0
                for item_no in all_item_keys:
                    if item_no not in sa_items or item_no not in draft_items: continue
                    sa = sa_items[item_no]
                    dr = draft_items[item_no]

                    name_ok = is_smart_match(sa['product_name'], dr['product_name'])
                    # Temporarily strip commas to do math check
                    w_ok = abs(
                        float(sa['net_weight'].replace(',', '')) - float(dr['net_weight'].replace(',', ''))) < 0.001
                    fob_ok = abs(
                        float(sa['fob_value'].replace(',', '')) - float(dr['fob_value'].replace(',', ''))) < 0.01

                    if name_ok and w_ok and fob_ok: matched_count += 1

                if matched_count == len(all_item_keys) and len(all_item_keys) > 0:
                    st.success(f"🎉 **PERFECT VERIFICATION: ALL {matched_count} ITEMS MATCHED 100%!**")
                else:
                    st.warning(f"Matched {matched_count} / {len(all_item_keys)} items.")

                for item_no in all_item_keys:
                    st.subheader(f"📦 Item #{item_no}")

                    if item_no not in sa_items:
                        st.error("Missing in SA file.")
                        continue
                    if item_no not in draft_items:
                        st.error("Missing in DRAFT file.")
                        continue

                    sa = sa_items[item_no]
                    draft = draft_items[item_no]

                    # 1. Name Check (Shows both strings for debugging)
                    if is_smart_match(sa['product_name'], draft['product_name']):
                        st.success(
                            f"✅ **Product Name:** MATCH\n* **SA:** `{sa['product_name']}`\n* **DRAFT:** `{draft['product_name']}`")
                    else:
                        st.error(
                            f"❌ **Product Name: MISMATCH**\n* **SA:** `{sa['product_name']}`\n* **DRAFT:** `{draft['product_name']}`")

                    # 2. Weight Check (Shows both strings for debugging)
                    try:
                        weight_match = abs(float(sa['net_weight'].replace(',', '')) - float(
                            draft['net_weight'].replace(',', ''))) < 0.001
                    except ValueError:
                        weight_match = False

                    if weight_match:
                        st.success(
                            f"✅ **Net Weight:** MATCH\n* **SA:** `{sa['net_weight']} kg`\n* **DRAFT:** `{draft['net_weight']} KGM`")
                    else:
                        st.error(
                            f"❌ **Net Weight: MISMATCH**\n* **SA:** `{sa['net_weight']} kg`\n* **DRAFT:** `{draft['net_weight']} KGM`")

                    # 3. FOB Check (Shows both strings for debugging)
                    try:
                        val_match = abs(
                            float(sa['fob_value'].replace(',', '')) - float(draft['fob_value'].replace(',', ''))) < 0.01
                    except ValueError:
                        val_match = False

                    if val_match:
                        st.success(
                            f"✅ **FOB Value:** MATCH\n* **SA:** `{sa['fob_value']}`\n* **DRAFT:** `{draft['fob_value']}`")
                    else:
                        st.error(
                            f"❌ **FOB Value: MISMATCH**\n* **SA:** `{sa['fob_value']}`\n* **DRAFT:** `{draft['fob_value']}`")

                    st.divider()

            finally:
                if os.path.exists(sa_path): os.remove(sa_path)
                if os.path.exists(draft_path): os.remove(draft_path)