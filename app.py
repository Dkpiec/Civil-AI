import streamlit as st
import ezdxf
import pandas as pd
import math
import re
import io
import requests
import os
import traceback

# --- API Keys ---
CONVERT_API_SECRET = os.environ.get("CONVERT_API_SECRET", "")

# --- Helper Functions ---
def get_midpoint(entity):
    """Calculates the geometric center of a CAD line/polyline."""
    try:
        if entity.dxftype() == 'LINE':
            return ((entity.dxf.start.x + entity.dxf.end.x) / 2, (entity.dxf.start.y + entity.dxf.end.y) / 2)
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            if not pts: return (0, 0)
            return pts[len(pts)//2]
    except Exception: return (0, 0)

def calculate_length(entity):
    """Calculates the total length of a CAD line/polyline in mm."""
    try:
        if entity.dxftype() == 'LINE':
            return math.dist((entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y))
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            if len(pts) < 2: return 0.0
            length = sum(math.dist(pts[i], pts[i+1]) for i in range(len(pts)-1))
            if entity.closed: length += math.dist(pts[-1], pts[0])
            return length
    except Exception: return 0.0

def process_file_bytes(uploaded_file):
    """Converts DWG to DXF if necessary and returns an ezdxf document."""
    file_bytes = uploaded_file.getvalue()
    file_ext = uploaded_file.name.split('.')[-1].lower()
    
    if file_ext == 'dwg':
        if not CONVERT_API_SECRET:
            st.error("ConvertAPI Secret missing. Cannot convert DWG.")
            return None
        url = f"https://v2.convertapi.com/convert/dwg/to/dxf?Secret={CONVERT_API_SECRET}"
        files = {'file': (uploaded_file.name, file_bytes)}
        res = requests.post(url, files=files)
        if res.status_code == 200:
            file_bytes = requests.get(res.json()['Files'][0]['Url']).content
        else: return None
        
    temp_path = f"temp_{uploaded_file.name}.dxf"
    with open(temp_path, "wb") as f: f.write(file_bytes)
    return ezdxf.readfile(temp_path)

# --- Universal Engineering Regex Patterns ---
# Matches beam names anywhere in a text string (e.g., "B1029A", "PB-12", "BEAM B101")
BEAM_PATTERN = r'\b(?:PB|B|RB|CB|TB|GB|FB|IB|MB|SB|BEAM)\s*[-_]?\s*[0-9]+[A-Za-z]?\b'

# STRICT Rebar Pattern: 
# Group 1 = Count (1-149)
# Separator = any combination of -, #, T, Y, X, @, /, or spaces
# Group 2 = STRICT STANDARD DIAMETERS (8, 10, 12, 16, 20, 25, 28, 32, 40)
REBAR_PATTERN = r'(?<!\d)(\d{1,3})\s*[-#@A-Za-z/]+\s*(0?6|0?8|10|12|14|16|20|25|28|32|40)(?!\d)'

# --- UI Setup ---
st.set_page_config(page_title="Universal Civil AI", page_icon="🏗️", layout="wide")
st.title("🏗️ Universal Civil AI: Deep Master Extractor")

st.markdown("Upload both your **Framing Plan** (for lengths) and your **Beam Details** (for rebars). The AI will cross-reference them to generate a complete BBS.")

uploaded_files = st.file_uploader("Upload CAD Drawings (Select Multiple Files)", type=[".dxf", ".dwg"], accept_multiple_files=True)

if uploaded_files:
    st.divider()
    st.subheader("1. Assign File Roles")
    file_names = [f.name for f in uploaded_files]
    
    col1, col2 = st.columns(2)
    with col1:
        # Allow selecting the same file for both if it's an all-in-one drawing
        framing_filename = st.selectbox("Which file is the Framing Plan? (Extracts Lengths)", ["None"] + file_names)
    with col2:
        detail_filename = st.selectbox("Which file is the Beam Details? (Extracts Rebars)", ["None"] + file_names)

    if framing_filename != "None" and detail_filename != "None":
        if st.button("🔍 Step 2: Deep Scan & Cross-Reference", type="primary"):
            with st.spinner("Executing Deep Nearest-Neighbor Analysis..."):
                try:
                    framing_file = next(f for f in uploaded_files if f.name == framing_filename)
                    detail_file = next(f for f in uploaded_files if f.name == detail_filename)
                    
                    doc_frame = process_file_bytes(framing_file)
                    doc_detail = process_file_bytes(detail_file)
                    
                    # ==========================================
                    # PHASE 1: FRAMING PLAN (LENGTH EXTRACTION)
                    # ==========================================
                    beam_lengths = {}
                    framing_nodes = []
                    
                    # 1A. Find all text that looks like a beam name
                    for text in doc_frame.modelspace().query('TEXT MTEXT'):
                        content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                        found_beams = re.findall(BEAM_PATTERN, content)
                        if found_beams:
                            try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                            except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                            
                            for b in found_beams:
                                framing_nodes.append({'beam': b.replace(" ", ""), 'pos': pos})
                            
                    # 1B. Map lines to the nearest beam text
                    for entity in doc_frame.modelspace().query('LINE LWPOLYLINE'):
                        layer = entity.dxf.layer.upper()
                        # Skip junk layers to speed up processing
                        if any(junk in layer for junk in ['GRID', 'DIM', 'TEXT', 'HATCH', 'DEFPOINTS']): continue
                            
                        length = calculate_length(entity)
                        if length > 300: # Filter out tiny irrelevant lines
                            midpoint = get_midpoint(entity)
                            
                            # Find the closest beam text to this line
                            closest_beam = None
                            min_dist = float('inf')
                            for fn in framing_nodes:
                                dist = math.dist(midpoint, fn['pos'])
                                if dist < 3000 and dist < min_dist: # 3m radius threshold
                                    min_dist = dist
                                    closest_beam = fn['beam']
                            
                            # Update the max length for this beam
                            if closest_beam:
                                if closest_beam not in beam_lengths or length > beam_lengths[closest_beam]:
                                    beam_lengths[closest_beam] = length

                    # ==========================================
                    # PHASE 2: DETAIL PLAN (REBAR EXTRACTION)
                    # ==========================================
                    detail_beam_nodes = []
                    rebar_nodes = []
                    
                    # 2A. Sort all text into "Beam Titles" and "Rebar Callouts"
                    for text in doc_detail.modelspace().query('TEXT MTEXT'):
                        content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                        try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                        except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                        
                        # Is it a Beam Title? (e.g. "BEAM B101, B102")
                        found_beams = re.findall(BEAM_PATTERN, content)
                        if found_beams:
                            cleaned_beams = [b.replace(" ", "") for b in found_beams]
                            detail_beam_nodes.append({'beams': cleaned_beams, 'pos': pos})
                            
                        # Is it a Rebar Callout? (e.g. "2-T25 + 2-T20")
                        found_rebars = list(re.finditer(REBAR_PATTERN, content))
                        if found_rebars:
                            extracted_bars = []
                            for match in found_rebars:
                                count = int(match.group(1))
                                dia = int(match.group(2))
                                if count < 150: # Final sanity check
                                    extracted_bars.append({'count': count, 'dia': dia})
                            
                            if extracted_bars:
                                rebar_nodes.append({'callout': content, 'pos': pos, 'bars': extracted_bars})

                    # 2B. Nearest-Neighbor Linking: Assign Rebars to the closest Beam Title
                    beam_rebars = {}
                    for rb in rebar_nodes:
                        closest_node = None
                        min_dist = float('inf')
                        
                        for dbn in detail_beam_nodes:
                            dist = math.dist(rb['pos'], dbn['pos'])
                            if dist < 20000 and dist < min_dist: # 20m wide net
                                min_dist = dist
                                closest_node = dbn
                                
                        if closest_node:
                            for b in closest_node['beams']:
                                if b not in beam_rebars:
                                    beam_rebars[b] = []
                                # Add all bars found in this specific text
                                for individual_bar in rb['bars']:
                                    beam_rebars[b].append({
                                        'callout': rb['callout'],
                                        'count': individual_bar['count'],
                                        'dia': individual_bar['dia']
                                    })

                    # Store results in session state
                    all_detected_beams = list(set(list(beam_lengths.keys()) + list(beam_rebars.keys())))
                    st.session_state["all_beams"] = sorted(all_detected_beams)
                    st.session_state["beam_lengths"] = beam_lengths
                    st.session_state["beam_rebars"] = beam_rebars
                    
                except Exception as e:
                    st.error(f"Error processing files: {e}")
                    st.code(traceback.format_exc())

    # --- Step 3: UI for Beam Selection and Output ---
    if "all_beams" in st.session_state:
        st.divider()
        st.subheader("3. Select Beams to Process")
        st.markdown(f"**{len(st.session_state['all_beams'])} unique beams detected across both files.**")
        
        selected_beams = st.multiselect("Review and select beams for the Excel report:", 
                                        options=st.session_state["all_beams"], 
                                        default=st.session_state["all_beams"])

        if st.button("✅ Step 4: Generate Master BBS Report", type="primary"):
            bbs_data = []
            
            for beam in selected_beams:
                length_mm = st.session_state["beam_lengths"].get(beam, 0.0) 
                rebars = st.session_state["beam_rebars"].get(beam, [])
                
                # Only add to report if it actually has rebars mapped to it
                for bar in rebars:
                    cutting_length = round(length_mm + (2 * (9 * bar['dia'])), 2) if length_mm > 0 else 0.0
                    
                    bbs_data.append({
                        'Member / Beam': beam,
                        'Bar Callout': bar['callout'],
                        'Diameter (mm)': bar['dia'],
                        'No. of Bars': bar['count'],
                        'Est. Cut Length (mm)': cutting_length,
                        'Total Length (m)': round((cutting_length * bar['count']) / 1000, 2),
                        'Total Weight (kg)': round(((bar['dia']**2)/162) * ((cutting_length * bar['count'])/1000), 2)
                    })
            
            df_bbs = pd.DataFrame(bbs_data)
            
            if not df_bbs.empty:
                st.success("Master Report generated successfully!")
                
                # Allow user to edit inline before downloading
                st.markdown("*Interactive Preview: You can double-click cells to adjust quantities before downloading.*")
                edited_df = st.data_editor(df_bbs, use_container_width=True)
                
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    edited_df.to_excel(writer, sheet_name='Master Linked BBS', index=False)
                
                st.download_button("📥 Download Master Excel Report", data=buffer.getvalue(), file_name="Master_BBS_Report.xlsx", type="primary")
            else:
                st.warning("No linked data could be generated. Ensure the beam names match between the framing and detail plans.")
