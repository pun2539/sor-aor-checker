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
    sa_extracted_total_nw = "0.000"
    sa_extracted_total_fob = "0.00"

    for p_idx, page_layout in enumerate(extract_pages(pdf_path)):

        boxes = [b for b in page_layout if isinstance(b, (LTTextBox, LTTextLine))]

        # 1. Product Names (Wider net: 45 to 75)
        names = []
        trade_boxes = [b for b in boxes if 45 < b.x0 < 75]
        for b in trade_boxes:
            txt = b.get_text()
            # Only process boxes that actually contain the product prefix
            if 'อาหารสัตว' in txt:
                split_items = re.split(r'อาหารสัตว[^\n\(]*', txt)
                for it in split_items:
                    clean = it.strip().replace('\n', ' ')
                    clean = re.sub(r'[\u0E00-\u0E7F\uF700-\uF71F]+', ' ', clean)
                    clean = clean.strip()
                    if clean.startswith('('):
                        clean = clean[1:]
                    if clean.endswith(')'):
                        clean = clean[:-1]
                    clean = re.sub(r'\s+', ' ', clean)

                    # Explicitly ignore header garbage that might get caught in the net
                    junk_keywords = ["Trade Name", "No.", "ชื่อทางการค้า", "่อทางการคา", "Date of Departure",
                                     "effem.com"]
                    if clean and not any(h in clean for h in junk_keywords):
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

        # --- EXTRACT SA GRAND TOTALS DIRECTLY ---
        # 1. Find the exact Y-coordinate of the word "รวม"
        ruam_y0 = None
        for b in boxes:
            if b.get_text().strip() == "รวม":
                ruam_y0 = b.y0
                break

        # 2. If we found "รวม", scan that exact horizontal line for the numbers
        if ruam_y0 is not None:
            for b in boxes:
                if abs(b.y0 - ruam_y0) < 5:
                    txt = b.get_text().strip()

                    # Grab Total Net Weight
                    if "กิโลกรัม" in txt or "KGM" in txt:
                        m_nw = re.search(r'([\d,]+\.\d{3})', txt)
                        if m_nw:
                            sa_extracted_total_nw = m_nw.group(1)

                        # If the FOB is smooshed into the same text box right after the weight (e.g. "16,958.880 กิโลกรัม 1,647,789.46")
                        m_fob = re.search(r'([\d,]+\.\d{2})$', txt)
                        if m_fob:
                            sa_extracted_total_fob = m_fob.group(1)

                    # Otherwise, grab Total FOB if it's sitting in its own separate text box
                    elif re.fullmatch(r'[\d,]+\.\d{2}', txt):
                        sa_extracted_total_fob = txt

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

    return sa_items, sa_extracted_total_nw, sa_extracted_total_fob


def parse_draft_native(pdf_path):
    """Parses digital Export Declaration (DRAFT) text using line-based extraction."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "".join(p.extract_text(layout=True) or "" for p in pdf.pages)

    items = {}
    nw_matches = re.findall(r'\bNW\s*=?\s*([\d,]+\.\d{3})\s*KGM', text, flags=re.IGNORECASE)
    # KEPT COMMA HERE
    total_nw = nw_matches[-1].strip() if nw_matches else "0.000"
    # Extract Grand Total FOB (Finds the absolute last currency value in the document)
    fob_matches = re.findall(r'(?:THB|AUD|USD|INR|IDR)\s*([\d,]+\.\d{2})', text, flags=re.IGNORECASE)
    total_fob = fob_matches[-1].strip() if fob_matches else "0.00"

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

        name = re.sub(r'[=\_!:\|]+', ' ', name)
        name = re.sub(r'^\s*[^A-Za-z0-9]+', '', name)
        name = re.sub(r'\s+', ' ', name).strip()
        name = name.replace(' &', '&').replace('& ', '&')

        # KEPT COMMAS IN DICTIONARY
        items[i + 1] = {
            'product_name': name,
            'net_weight': weight,
            'fob_value': value
        }

    return items, total_nw, total_fob


def is_smart_match(str1, str2):
    """Enforces strict 100% exact character match, ignoring ONLY spaces."""
    s1 = re.sub(r'\s+', '', str1)
    s2 = re.sub(r'\s+', '', str2)

    if not s1 or not s2:
        return False

    # STRICT EQUALITY ONLY. Removed the lenient "in" substring match.
    return s1 == s2


# --- STREAMLIT UI ---
st.set_page_config(layout="wide", page_title="Sor Aor vs Declaration Checker")
st.title("📄 Sor Aor vs Export Declaration (Batch Engine)")

# 1. Initialize Session State for Accumulating Files
if "file_queue" not in st.session_state:
    st.session_state.file_queue = {}
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

# 2. File Uploader (Using a dynamic key to allow clearing)
uploaded_batch = st.file_uploader(
    "Upload SA and DRAFT PDFs here (You can drag & drop from multiple folders!)",
    type=["pdf"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.uploader_key}"
)

# 3. Add newly dropped files to the persistent memory queue
if uploaded_batch:
    for f in uploaded_batch:
        if f.name not in st.session_state.file_queue:
            st.session_state.file_queue[f.name] = f

# 4. Queue Management & Processing UI
if st.session_state.file_queue:
    # Top Control Bar
    col1, col2 = st.columns([8, 2])
    with col1:
        st.info(f"📁 **Files currently in memory queue:** {len(st.session_state.file_queue)}")
    with col2:
        if st.button("🗑️ Clear All Files", use_container_width=True):
            st.session_state.file_queue = {}
            st.session_state.uploader_key += 1
            st.rerun()

    # Auto-Pairing Engine
    invoices = {}
    unrecognized = []

    for file_name, file_obj in st.session_state.file_queue.items():
        # Regex: Look for 'sa' or 'draft' at start, ignore spaces, grab the numbers
        match = re.search(r'^(sa|draft)\s*(\d+)', file_name, flags=re.IGNORECASE)
        if match:
            doc_type = match.group(1).lower()
            inv_num = match.group(2)

            if inv_num not in invoices:
                invoices[inv_num] = {}
            invoices[inv_num][doc_type] = file_obj
        else:
            unrecognized.append(file_name)

    # Filter Valid Pairs and Catch Orphans
    valid_pairs = {}
    missing_partners = []

    for inv_num, docs in invoices.items():
        if 'sa' in docs and 'draft' in docs:
            valid_pairs[inv_num] = docs
        else:
            missing_type = 'DRAFT' if 'sa' in docs else 'SA'
            missing_partners.append(f"**Invoice {inv_num}:** Missing {missing_type} file.")

    # Display Warnings for orphans/unrecognized files
    if missing_partners:
        st.warning("⚠️ **Skipped Unpaired Files:**\n\n" + "\n".join([f"- {m}" for m in missing_partners]))
    if unrecognized:
        st.error("⚠️ **Could not detect SA/Draft Invoice Number:**\n\n" + "\n".join([f"- `{u}`" for u in unrecognized]))

    # 5. Process Valid Pairs
    if valid_pairs:
        st.write("---")
        if st.button(f"🚀 Start Verifying {len(valid_pairs)} Invoice Pair(s)", type="primary"):

            invoice_results = {}

            # --- PHASE 1: PROCESS ALL FILES IN THE BACKGROUND ---
            with st.spinner("Extracting and analyzing all documents..."):
                # Sort invoice numbers numerically before processing
                for inv_num in sorted(valid_pairs.keys(), key=int):
                    docs = valid_pairs[inv_num]

                    # Save to temp files
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_sa:
                        tmp_sa.write(docs['sa'].getvalue())
                        sa_path = tmp_sa.name

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_draft:
                        tmp_draft.write(docs['draft'].getvalue())
                        draft_path = tmp_draft.name

                    try:
                        # Execute native parsers
                        sa_items, sa_total_nw, sa_total_fob = parse_sa_native(sa_path)
                        draft_items, draft_total_nw, draft_total_fob = parse_draft_native(draft_path)

                        # Evaluate Line Items
                        all_item_keys = sorted(set(list(sa_items.keys()) + list(draft_items.keys())))
                        matched_count = 0
                        mismatched_items = []

                        for item_no in all_item_keys:
                            clickable_link = f"<a href='#item-{item_no}' style='color: #cc0000; text-decoration: underline;'>#{item_no}</a>"

                            if item_no not in sa_items or item_no not in draft_items:
                                mismatched_items.append(clickable_link)
                                continue

                            sa = sa_items[item_no]
                            dr = draft_items[item_no]

                            name_ok = is_smart_match(sa['product_name'], dr['product_name'])
                            try:
                                w_ok = abs(float(sa['net_weight'].replace(',', '')) - float(
                                    dr['net_weight'].replace(',', ''))) < 0.001
                            except ValueError:
                                w_ok = False
                            try:
                                fob_ok = abs(float(sa['fob_value'].replace(',', '')) - float(
                                    dr['fob_value'].replace(',', ''))) < 0.01
                            except ValueError:
                                fob_ok = False

                            if name_ok and w_ok and fob_ok:
                                matched_count += 1
                            else:
                                mismatched_items.append(clickable_link)

                        # Evaluate Grand Totals
                        try:
                            sa_nw_float = float(sa_total_nw.replace(',', ''))
                            draft_nw_float = float(draft_total_nw.replace(',', ''))
                            total_nw_match = abs(sa_nw_float - draft_nw_float) < 0.001
                        except ValueError:
                            total_nw_match = False
                            draft_nw_float = 0

                        try:
                            sa_fob_float = float(sa_total_fob.replace(',', ''))
                            draft_fob_float = float(draft_total_fob.replace(',', ''))
                            fob_diff = round(abs(sa_fob_float - draft_fob_float), 2)
                            total_fob_match = fob_diff <= 0.03
                        except ValueError:
                            total_fob_match = False
                            fob_diff = 0.00

                        # Determine if the ENTIRE invoice is perfect
                        items_perfect = (matched_count == len(all_item_keys) and len(all_item_keys) > 0)
                        totals_perfect = (total_nw_match and draft_nw_float > 0 and total_fob_match)
                        is_perfect = items_perfect and totals_perfect

                        # Save all calculated data for the UI generation
                        invoice_results[inv_num] = {
                            'sa_items': sa_items, 'draft_items': draft_items,
                            'sa_total_nw': sa_total_nw, 'draft_total_nw': draft_total_nw,
                            'sa_total_fob': sa_total_fob, 'draft_total_fob': draft_total_fob,
                            'all_item_keys': all_item_keys, 'matched_count': matched_count,
                            'mismatched_items': mismatched_items, 'total_nw_match': total_nw_match,
                            'draft_nw_float': draft_nw_float, 'total_fob_match': total_fob_match,
                            'fob_diff': fob_diff, 'is_perfect': is_perfect
                        }

                    finally:
                        if os.path.exists(sa_path): os.remove(sa_path)
                        if os.path.exists(draft_path): os.remove(draft_path)

            # --- PHASE 2: GENERATE THE UI ---

            # Create dynamic Tab names with Icons
            tab_names = []
            for inv, data in invoice_results.items():
                icon = "✅" if data['is_perfect'] else "❌"
                tab_names.append(f"{icon} {inv}")

            tabs = st.tabs(tab_names)

            # Populate the Tabs with the saved data
            for tab, (inv_num, data) in zip(tabs, invoice_results.items()):
                with tab:
                    # --- RAW DATA DEBUGGER ---
                    with st.expander("🛠️ View Raw Extracted Data (Debugging)"):
                        st.write("**Extracted SA Items:**", data['sa_items'])
                        st.write("**Extracted DRAFT Items:**", data['draft_items'])

                    # --- 1. LINE ITEMS SUMMARY (UNIFIED CONTAINER) ---
                    if data['matched_count'] == len(data['all_item_keys']) and len(data['all_item_keys']) > 0:
                        summary_html = f"""
                        <div style='background-color: #e6ffe6; border: 2px solid #00cc44; border-radius: 10px; padding: 20px; text-align: center; margin-bottom: 20px;'>
                            <h2 style='color: #00cc44; margin-top: 0;'>🔍 LINE ITEMS COMPARISON 🔍</h2>
                            <p style='font-size: 18px; color: #00802b; font-weight: bold; margin-bottom: 0;'>🎉 PERFECT VERIFICATION: ALL {data['matched_count']} ITEMS MATCHED 100%!</p>
                        </div>
                        """
                    else:
                        mismatch_str = ", ".join(data['mismatched_items'])
                        summary_html = f"""
                        <div style='background-color: #ffe6e6; border: 2px solid #ff4b4b; border-radius: 10px; padding: 20px; text-align: center; margin-bottom: 20px;'>
                            <h2 style='color: #ff4b4b; margin-top: 0;'>🔍 LINE ITEMS COMPARISON 🔍</h2>
                            <p style='font-size: 18px; color: #cc0000; font-weight: bold;'>⚠️ Matched {data['matched_count']} / {len(data['all_item_keys'])} items.</p>
                            <p style='font-size: 16px; color: #cc0000; margin-bottom: 0;'>❌ <b>Mismatched Item(s):</b> {mismatch_str}</p>
                        </div>
                        """
                    st.markdown(summary_html, unsafe_allow_html=True)

                    # --- 2. GRAND TOTAL VERIFICATION (UNIFIED CONTAINER) ---
                    if data['total_nw_match'] and data['draft_nw_float'] > 0 and data['total_fob_match']:
                        total_html = f"""
                        <div style='background-color: #e0f2fe; border: 2px solid #0083b8; border-radius: 10px; padding: 20px; text-align: center; margin-bottom: 30px;'>
                            <h2 style='color: #0083b8; margin-top: 0;'>📊 GRAND TOTAL VERIFICATION 📊</h2>
                            <p style='font-size: 18px; color: #005980; font-weight: bold; margin-bottom: 5px;'>✅ Grand Total Net Weight: MATCH</p>
                            <p style='font-size: 16px; color: #005980; margin-bottom: 15px;'><b>SA Stated:</b> {data['sa_total_nw']} kg &nbsp;&nbsp;|&nbsp;&nbsp; <b>DRAFT Stated:</b> {data['draft_total_nw']} KGM</p>
                            <p style='font-size: 18px; color: #005980; font-weight: bold; margin-bottom: 5px;'>✅ Grand Total FOB: MATCH <span style='font-size: 14px; font-weight: normal;'>(Difference is {data['fob_diff']:.2f}, within 0.03 tolerance)</span></p>
                            <p style='font-size: 16px; color: #005980; margin-bottom: 0;'><b>SA Stated:</b> {data['sa_total_fob']} &nbsp;&nbsp;|&nbsp;&nbsp; <b>DRAFT Stated:</b> {data['draft_total_fob']}</p>
                        </div>
                        """
                    else:
                        nw_icon = "✅" if (data['total_nw_match'] and data['draft_nw_float'] > 0) else "❌"
                        nw_label = "MATCH" if (
                                    data['total_nw_match'] and data['draft_nw_float'] > 0) else "MISMATCH"
                        nw_color = "#00802b" if (
                                    data['total_nw_match'] and data['draft_nw_float'] > 0) else "#cc0000"

                        fob_icon = "✅" if data['total_fob_match'] else "❌"
                        fob_label = "MATCH" if data['total_fob_match'] else "MISMATCH"
                        fob_color = "#00802b" if data['total_fob_match'] else "#cc0000"

                        if data['total_fob_match']:
                            fob_note = f"<span style='font-size: 14px; font-weight: normal;'>(Difference is {data['fob_diff']:.2f}, within 0.03 tolerance)</span>"
                        else:
                            fob_note = f"<span style='font-size: 14px; font-weight: normal;'>(Difference is {data['fob_diff']:.2f}, exceeding 0.03 tolerance!)</span>"

                        total_html = f"""
                        <div style='background-color: #ffe6e6; border: 2px solid #ff4b4b; border-radius: 10px; padding: 20px; text-align: center; margin-bottom: 30px;'>
                            <h2 style='color: #ff4b4b; margin-top: 0;'>📊 GRAND TOTAL VERIFICATION 📊</h2>
                            <p style='font-size: 18px; color: {nw_color}; font-weight: bold; margin-bottom: 5px;'>{nw_icon} Grand Total Net Weight: {nw_label}</p>
                            <p style='font-size: 16px; color: {nw_color}; margin-bottom: 15px;'><b>SA Stated:</b> {data['sa_total_nw']} kg &nbsp;&nbsp;|&nbsp;&nbsp; <b>DRAFT Stated:</b> {data['draft_total_nw']} KGM</p>
                            <p style='font-size: 18px; color: {fob_color}; font-weight: bold; margin-bottom: 5px;'>{fob_icon} Grand Total FOB: {fob_label} {fob_note}</p>
                            <p style='font-size: 16px; color: {fob_color}; margin-bottom: 0;'><b>SA Stated:</b> {data['sa_total_fob']} &nbsp;&nbsp;|&nbsp;&nbsp; <b>DRAFT Stated:</b> {data['draft_total_fob']}</p>
                        </div>
                        """
                    st.markdown(total_html, unsafe_allow_html=True)

                    st.divider()

                    # --- 3. DETAILED ITEM BREAKDOWN (BOTTOM) ---
                    for item_no in data['all_item_keys']:
                        st.subheader(f"📦 Item #{item_no}")

                        if item_no not in data['sa_items']:
                            st.error("Missing in SA file.")
                            continue
                        if item_no not in data['draft_items']:
                            st.error("Missing in DRAFT file.")
                            continue

                        sa = data['sa_items'][item_no]
                        draft = data['draft_items'][item_no]

                        # 1. Name Check
                        if is_smart_match(sa['product_name'], draft['product_name']):
                            st.success(
                                f"✅ **Product Name:** MATCH\n* **SA:** `{sa['product_name']}`\n* **DRAFT:** `{draft['product_name']}`")
                        else:
                            st.error(
                                f"❌ **Product Name: MISMATCH**\n* **SA:** `{sa['product_name']}`\n* **DRAFT:** `{draft['product_name']}`")

                        # 2. Weight Check
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

                        # 3. FOB Check
                        try:
                            val_match = abs(float(sa['fob_value'].replace(',', '')) - float(
                                draft['fob_value'].replace(',', ''))) < 0.01
                        except ValueError:
                            val_match = False

                        if val_match:
                            st.success(
                                f"✅ **FOB Value:** MATCH\n* **SA:** `{sa['fob_value']}`\n* **DRAFT:** `{draft['fob_value']}`")
                        else:
                            st.error(
                                f"❌ **FOB Value: MISMATCH**\n* **SA:** `{sa['fob_value']}`\n* **DRAFT:** `{draft['fob_value']}`")

                        st.divider()
