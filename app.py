import streamlit as st
import ezdxf
import pandas as pd
import math
import re
import io
import requests
import os
import json
import traceback
from google import genai

# --- API Keys ---
CONVERT_API_SECRET = os.environ.get("CONVERT_API_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

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

def ask_ai_to_parse_details(beam_clusters):
    """Sends clusters of CAD text to Gemini to extract main bars, stirrups, and dimensions."""
    if not GEMINI_API_KEY: return {}
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = """
    You are an expert Civil Structural Engineer. I am giving you raw text extracted from CAD detail drawings for various beams.
    Analyze the text clusters and extract the structural details. 
    
    Return EXACTLY a JSON dictionary where keys are Beam Names, and values are objects containing:
    - "width": (integer, in mm) e.g., 230
    - "depth": (integer, in mm) e.g., 450
    - "main_bars": array of objects [{"count": int, "dia": int}, ...]
    - "stirrups": array of objects [{"dia": int, "spacing": int}, ...]
    
    If data is missing, use 0 or empty arrays. 
    Look for dimensions like "230X450" or "230x600".
    Look for main bars like "2-T20", "3-25#", "4 dia 16".
    Look for stirrups (rings) like "8T10 @ 150c/c", "10# @ 200", "8 dia 150 c/c".
    
    Raw Data:
    """ + json.dumps(beam_clusters)

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"AI Parsing Error: {e}")
        return {}

# --- Regex Patterns ---
BEAM_PATTERN = r'\b(?:PB|B|RB|CB|TB|GB|FB|IB|MB|SB|BEAM)\s*[-_]?\s*[0-9]+[A-Za-z]?\b'

# --- UI Setup ---
st.set_page_config(page_title="AI Civil Extractor", page_icon="🏗️", layout="wide")
st.title("🏗️ AI Civil Extractor: Quantities & BBS")

uploaded_files = st.file_uploader("Upload Framing Plan & Detail Drawings (.dxf/.dwg)", type=[".dxf", ".dwg"], accept_multiple_files=True)

if uploaded_files:
    col1, col2 = st.columns(2)
    with col1: framing_filename = st.selectbox("Select Framing Plan (For Master List & Lengths)", ["None"] + [f.name for f in uploaded_files])
    with col2: detail_filename = st.selectbox("Select Details Plan (For Rebars, Stirrups, & Sections)", ["None"] + [f.name for f in uploaded_files])

    if framing_filename != "None" and detail_filename != "None" and st.button("🚀 Process Project Data", type="primary"):
        with st.spinner("Extracting CAD geometries & Engaging Gemini AI..."):
            try:
                doc_frame = process_file_bytes(next(f for f in uploaded_files if f.name == framing_filename))
                doc_detail = process_file_bytes(next(f for f in uploaded_files if f.name == detail_filename))
                
                # ==========================================
                # PHASE 1: FRAMING PLAN (MASTER LIST)
                # ==========================================
                master_beams = {} # {beam_name: {'length': 0.0}}
                framing_nodes = []
                
                for text in doc_frame.modelspace().query('TEXT MTEXT'):
                    content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                    found_beams = set(re.findall(BEAM_PATTERN, content))
                    for b in found_beams:
                        clean_b = b.replace(" ", "")
                        master_beams[clean_b] = {'length': 0.0} # Initialize
                        try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                        except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                        framing_nodes.append({'beam': clean_b, 'pos': pos})

                # Wide Net Line Matching (Find lengths)
                for entity in doc_frame.modelspace().query('LINE LWPOLYLINE'):
                    layer = entity.dxf.layer.upper()
                    if any(x in layer for x in ['GRID', 'DIM', 'TEXT', 'HATCH', 'DEFPOINTS']): continue
                        
                    length = calculate_length(entity)
                    if length > 500: # Beams are rarely under 500mm
                        midpoint = get_midpoint(entity)
                        for fn in framing_nodes:
                            if math.dist(midpoint, fn['pos']) < 4000: # 4m tolerance for messy CAD
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
                        # Gather all text within 15 meters of this beam title
                        for other_dt in detail_texts:
                            if math.dist(dt['pos'], other_dt['pos']) < 15000:
                                if len(other_dt['content']) > 2: # Ignore single letters
                                    beam_clusters[clean_b].append(other_dt['content'])
                        # Remove duplicates from cluster
                        beam_clusters[clean_b] = list(set(beam_clusters[clean_b]))

                # ==========================================
                # PHASE 3: AI SEMANTIC PARSING
                # ==========================================
                # Batch request to Gemini (keeps API calls low)
                ai_extracted_data = {}
                if beam_clusters:
                    st.info(f"🧠 Sending {len(beam_clusters)} beam detail clusters to AI for reading...")
                    
                    # Split into chunks if there are hundreds of beams to avoid payload limits
                    chunk_size = 50
                    cluster_items = list(beam_clusters.items())
                    for i in range(0, len(cluster_items), chunk_size):
                        chunk = dict(cluster_items[i:i + chunk_size])
                        parsed_chunk = ask_ai_to_parse_details(chunk)
                        ai_extracted_data.update(parsed_chunk)

                # ==========================================
                # PHASE 4: CALCULATION & REPORTING
                # ==========================================
                report_data = []
                cover_mm = 30 # Standard beam cover
                
                for beam, data in master_beams.items():
                    length_mm = data['length']
                    length_m = length_mm / 1000.0
                    
                    ai_data = ai_extracted_data.get(beam, {})
                    width = ai_data.get('width', 0)
                    depth = ai_data.get('depth', 0)
                    
                    # Concrete QTY
                    vol_m3 = round((width/1000.0) * (depth/1000.0) * length_m, 3) if width and depth else 0.0
                    
                    # Main Bars
                    for mb in ai_data.get('main_bars', []):
                        if mb['count'] > 0 and mb['dia'] > 0:
                            cut_length_m = round(length_m + (2 * (9 * mb['dia'] / 1000.0)), 2)
                            report_data.append({
                                'Beam': beam, 'Type': 'Main Bar', 'Dimension': f"{width}x{depth}" if width else "Unknown",
                                'Length (m)': round(length_m, 2), 'Concrete Vol (m3)': vol_m3,
                                'Dia (mm)': mb['dia'], 'Count': mb['count'], 'Spacing (mm)': '-',
                                'Cut Length (m)': cut_length_m,
                                'Total Weight (kg)': round(((mb['dia']**2)/162) * (cut_length_m * mb['count']), 2)
                            })
                            vol_m3 = "" # Blank out for subsequent rows of the same beam
                            
                    # Stirrups (Rings)
                    for stp in ai_data.get('stirrups', []):
                        if stp['dia'] > 0 and stp['spacing'] > 0 and width > 0 and depth > 0:
                            num_stirrups = int((length_mm / stp['spacing']) + 1) if length_mm > 0 else 0
                            
                            # Stirrup Cut Length = Perimeter of stirrup + 2 Hooks (10d)
                            # Perimeter = 2 * ((Width - 2*Cover) + (Depth - 2*Cover))
                            a = width - (2 * cover_mm)
                            b = depth - (2 * cover_mm)
                            stirrup_cut_mm = (2 * (a + b)) + (24 * stp['dia'])
                            stirrup_cut_m = round(stirrup_cut_mm / 1000.0, 2)
                            
                            report_data.append({
                                'Beam': beam, 'Type': 'Stirrup', 'Dimension': f"{width}x{depth}",
                                'Length (m)': round(length_m, 2), 'Concrete Vol (m3)': vol_m3,
                                'Dia (mm)': stp['dia'], 'Count': num_stirrups, 'Spacing (mm)': stp['spacing'],
                                'Cut Length (m)': stirrup_cut_m,
                                'Total Weight (kg)': round(((stp['dia']**2)/162) * (stirrup_cut_m * num_stirrups), 2)
                            })
                            vol_m3 = ""
                            
                    # If beam was found but AI couldn't parse details
                    if not ai_data.get('main_bars') and not ai_data.get('stirrups'):
                        report_data.append({
                            'Beam': beam, 'Type': 'Missing Details', 'Dimension': f"{width}x{depth}" if width else "Unknown",
                            'Length (m)': round(length_m, 2), 'Concrete Vol (m3)': vol_m3,
                            'Dia (mm)': 0, 'Count': 0, 'Spacing (mm)': '-', 'Cut Length (m)': 0, 'Total Weight (kg)': 0.0
                        })

                df_report = pd.DataFrame(report_data)
                
                if not df_report.empty:
                    st.success(f"✅ Master Extraction Complete! Analyzed {len(master_beams)} total beams.")
                    st.dataframe(df_report, use_container_width=True)
                    
                    buffer = io.BytesIO()
                    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                        df_report.to_excel(writer, sheet_name='AI Quantities', index=False)
                    
                    st.download_button("📥 Download Master Quantities Excel", data=buffer.getvalue(), file_name="AI_Quantities_Report.xlsx", type="primary")
                else:
                    st.warning("Could not map data. Please check files and CAD layer cleanliness.")
                    
            except Exception as e:
                st.error(f"Critical System Error: {e}")
                st.code(traceback.format_exc())
