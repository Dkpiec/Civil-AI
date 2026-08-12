import streamlit as st
import ezdxf
import pandas as pd
import math
import re
import io
import requests
import os
import traceback

# --- API Keys (Only required if uploading .dwg files via ConvertAPI) ---
CONVERT_API_SECRET = os.environ.get("CONVERT_API_SECRET", "")

# --- Helper Functions ---
def get_midpoint(entity):
    try:
        if entity.dxftype() == 'LINE': return ((entity.dxf.start.x + entity.dxf.end.x) / 2, (entity.dxf.start.y + entity.dxf.end.y) / 2)
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            return pts[len(pts)//2] if pts else (0, 0)
    except: return (0, 0)

def calculate_length(entity):
    try:
        if entity.dxftype() == 'LINE': return math.dist((entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y))
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            if len(pts) < 2: return 0.0
            length = sum(math.dist(pts[i], pts[i+1]) for i in range(len(pts)-1))
            if entity.closed: length += math.dist(pts[-1], pts[0])
            return length
    except: return 0.0

def process_file_bytes(uploaded_file):
    file_bytes = uploaded_file.getvalue()
    if uploaded_file.name.lower().endswith('.dwg'):
        if not CONVERT_API_SECRET: return None
        url = f"https://v2.convertapi.com/convert/dwg/to/dxf?Secret={CONVERT_API_SECRET}"
        res = requests.post(url, files={'file': (uploaded_file.name, file_bytes)})
        if res.status_code == 200: file_bytes = requests.get(res.json()['Files'][0]['Url']).content
        else: return None
    temp_path = f"temp_{uploaded_file.name}.dxf"
    with open(temp_path, "wb") as f: f.write(file_bytes)
    return ezdxf.readfile(temp_path)

# --- Algorithmic Parsers ---
BEAM_PATTERN = r'\b(?:PB|B|RB|CB|TB|GB|FB|IB|MB|SB|BEAM)\s*[-_]?\s*[0-9]+[A-Za-z]?\b'
STANDARD_DIAS = {8, 10, 12, 16, 20, 25, 28, 32, 40}

def parse_beam_cluster_text(text_list):
    """Deterministically extracts dimensions, main bars, and stirrups from a text cluster."""
    width, depth = 0, 0
    main_bars = []
    stirrups = []
    
    for t in text_list:
        t_upper = t.upper()
        
        # 1. Look for cross-sections (e.g., 230X450, 300 x 600)
        dim_match = re.search(r'(\d{2,3})\s*[xX]\s*(\d{2,3})', t_upper)
        if dim_match and not width:
            w_val, d_val = int(dim_match.group(1)), int(dim_match.group(2))
            if 100 <= w_val <= 1000 and 150 <= d_val <= 3000:
                width, depth = w_val, d_val
                
        # 2. Look for Stirrups / Links (Contains '@' or 'C/C' or 'C-C')
        if '@' in t_upper or 'C/C' in t_upper or 'C-C' in t_upper:
            # Matches formats like: 8-T10 @ 150 or T10@150
            stp_match = re.search(r'(\d+)?\s*[-#T]?(\d{2})\s*@\s*(\d+)', t_upper)
            if stp_match:
                count = int(stp_match.group(1)) if stp_match.group(1) else 0
                dia = int(stp_match.group(2))
                spacing = int(stp_match.group(3))
                if dia in STANDARD_DIAS and 50 <= spacing <= 500:
                    stirrups.append({'dia': dia, 'spacing': spacing, 'count': count})
        else:
            # 3. Look for Main Bars (e.g., 2-T25, 2-20, 4#16)
            bar_matches = list(re.finditer(r'(?<!\d)(\d{1,2})\s*[-#TXY]\s*(\d{2})(?!\d)', t_upper))
            for bm in bar_matches:
                cnt = int(bm.group(1))
                d = int(bm.group(2))
                if d in STANDARD_DIAS and cnt < 50:
                    # Avoid duplicates
                    if {'count': cnt, 'dia': d} not in main_bars:
                        main_bars.append({'count': cnt, 'dia': d})
                        
    return width, depth, main_bars, stirrups

# --- UI Setup ---
st.set_page_config(page_title="Deterministic Civil Extractor", page_icon="🏗️", layout="wide")
st.title("🏗️ Algorithmic Civil Extractor: Quantities & BBS")
st.markdown("Extracts beam lengths, cross-sections, main reinforcement, and stirrups deterministically using spatial clustering and strict engineering RegEx rules.")

uploaded_files = st.file_uploader("Upload Framing Plan & Detail Drawings (.dxf/.dwg)", type=[".dxf", ".dwg"], accept_multiple_files=True)

if uploaded_files:
    col1, col2 = st.columns(2)
    with col1: framing_filename = st.selectbox("Select Framing Plan (For Master List & Lengths)", ["None"] + [f.name for f in uploaded_files])
    with col2: detail_filename = st.selectbox("Select Details Plan (For Rebars, Stirrups, & Sections)", ["None"] + [f.name for f in uploaded_files])

    if framing_filename != "None" and detail_filename != "None" and st.button("🚀 Process Project Data", type="primary"):
        with st.spinner("Extracting CAD geometries & parsing structural elements..."):
            try:
                doc_frame = process_file_bytes(next(f for f in uploaded_files if f.name == framing_filename))
                doc_detail = process_file_bytes(next(f for f in uploaded_files if f.name == detail_filename))
                
                # ==========================================
                # PHASE 1: FRAMING PLAN (MASTER LIST & LENGTHS)
                # ==========================================
                master_beams = {} 
                framing_nodes = []
                
                for text in doc_frame.modelspace().query('TEXT MTEXT'):
                    content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                    found_beams = set(re.findall(BEAM_PATTERN, content))
                    for b in found_beams:
                        clean_b = b.replace(" ", "")
                        master_beams[clean_b] = {'length': 0.0}
                        try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                        except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                        framing_nodes.append({'beam': clean_b, 'pos': pos})

                for entity in doc_frame.modelspace().query('LINE LWPOLYLINE'):
                    layer = entity.dxf.layer.upper()
                    if any(x in layer for x in ['GRID', 'DIM', 'TEXT', 'HATCH', 'DEFPOINTS']): continue
                        
                    length = calculate_length(entity)
                    if length > 500:
                        midpoint = get_midpoint(entity)
                        for fn in framing_nodes:
                            if math.dist(midpoint, fn['pos']) < 4000:
                                if length > master_beams[fn['beam']]['length']:
                                    master_beams[fn['beam']]['length'] = length

                # ==========================================
                # PHASE 2: DETAIL PLAN (SPATIAL CLUSTERING)
                # ==========================================
                detail_texts = []
                for text in doc_detail.modelspace().query('TEXT MTEXT'):
                    content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                    try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                    except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                    detail_texts.append({'content': content, 'pos': pos})

                beam_clusters = {}
                for dt in detail_texts:
                    found_beams = set(re.findall(BEAM_PATTERN, dt['content']))
                    for b in found_beams:
                        clean_b = b.replace(" ", "")
                        if clean_b not in beam_clusters:
                            beam_clusters[clean_b] = []
                        for other_dt in detail_texts:
                            if math.dist(dt['pos'], other_dt['pos']) < 15000:
                                if len(other_dt['content']) > 1:
                                    beam_clusters[clean_b].append(other_dt['content'])
                        beam_clusters[clean_b] = list(set(beam_clusters[clean_b]))

                # ==========================================
                # PHASE 3: ALGORITHMIC PARSING & CALCULATIONS
                # ==========================================
                report_data = []
                cover_mm = 30
                
                for beam, data in master_beams.items():
                    length_mm = data['length']
                    length_m = length_mm / 1000.0
                    
                    cluster_texts = beam_clusters.get(beam, [])
                    width, depth, main_bars, stirrups = parse_beam_cluster_text(cluster_texts)
                    
                    vol_m3 = round((width/1000.0) * (depth/1000.0) * length_m, 3) if width and depth else 0.0
                    
                    # Main Bars Entry
                    for mb in main_bars:
                        cut_length_m = round(length_m + (2 * (9 * mb['dia'] / 1000.0)), 2) if length_m > 0 else 0.0
                        report_data.append({
                            'Beam': beam, 'Type': 'Main Bar', 'Dimension': f"{width}x{depth}" if width else "Unknown",
                            'Length (m)': round(length_m, 2), 'Concrete Vol (m3)': vol_m3,
                            'Dia (mm)': mb['dia'], 'Count': mb['count'], 'Spacing (mm)': '-',
                            'Cut Length (m)': cut_length_m,
                            'Total Weight (kg)': round(((mb['dia']**2)/162) * (cut_length_m * mb['count']), 2)
                        })
                        vol_m3 = "" # Blank out concrete volume for subsequent rows of the same beam
                        
                    # Stirrups Entry
                    for stp in stirrups:
                        num_stirrups = int((length_mm / stp['spacing']) + 1) if length_mm > 0 and stp['spacing'] > 0 else 0
                        if width > 0 and depth > 0:
                            a = width - (2 * cover_mm)
                            b = depth - (2 * cover_mm)
                            stirrup_cut_mm = (2 * (a + b)) + (24 * stp['dia'])
                            stirrup_cut_m = round(stirrup_cut_mm / 1000.0, 2)
                        else:
                            stirrup_cut_m = 1.0 # Fallback estimate
                            
                        report_data.append({
                            'Beam': beam, 'Type': 'Stirrup', 'Dimension': f"{width}x{depth}" if width else "Unknown",
                            'Length (m)': round(length_m, 2), 'Concrete Vol (m3)': vol_m3,
                            'Dia (mm)': stp['dia'], 'Count': num_stirrups if num_stirrups > 0 else stp['count'], 
                            'Spacing (mm)': stp['spacing'],
                            'Cut Length (m)': stirrup_cut_m,
                            'Total Weight (kg)': round(((stp['dia']**2)/162) * (stirrup_cut_m * (num_stirrups if num_stirrups > 0 else stp['count'])), 2)
                        })
                        vol_m3 = ""
                        
                    # Fallback if no reinforcement was matched
                    if not main_bars and not stirrups:
                        report_data.append({
                            'Beam': beam, 'Type': 'Missing Details', 'Dimension': f"{width}x{depth}" if width else "Unknown",
                            'Length (m)': round(length_m, 2), 'Concrete Vol (m3)': vol_m3 if vol_m3 else 0.0,
                            'Dia (mm)': 0, 'Count': 0, 'Spacing (mm)': '-', 'Cut Length (m)': 0, 'Total Weight (kg)': 0.0
                        })

                df_report = pd.DataFrame(report_data)
                
                if not df_report.empty:
                    st.success(f"✅ Extraction Complete! Analyzed {len(master_beams)} total beams deterministically.")
                    st.dataframe(df_report, use_container_width=True)
                    
                    buffer = io.BytesIO()
                    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                        df_report.to_excel(writer, sheet_name='Quantities & BBS', index=False)
                    
                    st.download_button("📥 Download Excel Report", data=buffer.getvalue(), file_name="Deterministic_BBS_Report.xlsx", type="primary")
                else:
                    st.warning("Could not map data. Please check files and CAD layer cleanliness.")
                    
            except Exception as e:
                st.error(f"Critical System Error: {e}")
                st.code(traceback.format_exc())
