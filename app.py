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
    try:
        if entity.dxftype() == 'LINE':
            return ((entity.dxf.start.x + entity.dxf.end.x) / 2, (entity.dxf.start.y + entity.dxf.end.y) / 2)
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            if not pts: return (0, 0)
            return pts[len(pts)//2]
    except Exception: return (0, 0)

def calculate_length(entity):
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

# --- Robust Civil Engineering Regex ---
# Catches B1, PB-2, RB 12, BEAM-5, B12A, FB1, GB2
BEAM_REGEX = r'^(?:PB|B|RB|CB|TB|GB|FB|BEAM)\s*[-]?\s*\d+[A-Z]?$'

# --- UI Setup ---
st.set_page_config(page_title="Universal Civil AI", page_icon="🏗️", layout="wide")
st.title("🏗️ Universal Civil AI: Multi-File Linked BBS")

uploaded_files = st.file_uploader("Upload CAD Drawings (Select Multiple Files)", type=[".dxf", ".dwg"], accept_multiple_files=True)

if uploaded_files:
    st.divider()
    st.subheader("1. Assign File Roles")
    file_names = [f.name for f in uploaded_files]
    
    col1, col2 = st.columns(2)
    with col1:
        framing_filename = st.selectbox("Which file is the Framing Plan? (For Beam Lengths)", ["None"] + file_names)
    with col2:
        detail_filename = st.selectbox("Which file is the Beam Details? (For Rebars)", ["None"] + file_names)

    if framing_filename != "None" and detail_filename != "None":
        if st.button("🔍 Step 2: Scan Files for Beams", type="primary"):
            with st.spinner("Extracting data from both files..."):
                try:
                    # Parse both files
                    framing_file = next(f for f in uploaded_files if f.name == framing_filename)
                    detail_file = next(f for f in uploaded_files if f.name == detail_filename)
                    
                    doc_frame = process_file_bytes(framing_file)
                    doc_detail = process_file_bytes(detail_file)
                    
                    # --- Extract Beam Lengths from Framing Plan ---
                    beam_lengths = {}
                    framing_texts = []
                    
                    for text in doc_frame.modelspace().query('TEXT MTEXT'):
                        content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                        if re.match(BEAM_REGEX, content):
                            try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                            except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                            framing_texts.append({'beam': content, 'pos': pos})
                            
                    for entity in doc_frame.modelspace().query('LINE LWPOLYLINE'):
                        length = calculate_length(entity)
                        if length > 300: # Filter out tiny lines
                            midpoint = get_midpoint(entity)
                            for ft in framing_texts:
                                # If line is close to beam text, assign its length to the beam
                                if math.dist(midpoint, ft['pos']) < 2500: 
                                    # Keep the longest line associated with this beam name
                                    if ft['beam'] not in beam_lengths or length > beam_lengths[ft['beam']]:
                                        beam_lengths[ft['beam']] = length

                    # --- Extract Rebars from Details Plan ---
                    beam_rebars = {}
                    detail_texts = []
                    
                    for text in doc_detail.modelspace().query('TEXT MTEXT'):
                        content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                        try: pos = (text.dxf.insert.x, text.dxf.insert.y)
                        except AttributeError: pos = (text.dxf.align_point.x, text.dxf.align_point.y)
                        detail_texts.append({'content': content, 'pos': pos})
                    
                    # Map rebars to beams based on proximity in the detail drawing
                    for dt in detail_texts:
                        if re.match(BEAM_REGEX, dt['content']):
                            beam_name = dt['content']
                            beam_rebars[beam_name] = []
                            
                            # Find all rebar callouts near this beam name
                            for other_text in detail_texts:
                                if math.dist(dt['pos'], other_text['pos']) < 6000: # Search radius for rebars
                                    rebar_matches = list(re.finditer(r'(?<!\d)(\d{1,3})\s*[-#TXY]\s*(\d{2})(?!\d)', other_text['content']))
                                    for match in rebar_matches:
                                        if int(match.group(1)) < 150: # Ignore false dimensions
                                            beam_rebars[beam_name].append({
                                                'callout': other_text['content'],
                                                'count': int(match.group(1)),
                                                'dia': int(match.group(2))
                                            })

                    # Save to session state for the UI
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

        if st.button("✅ Step 4: Generate Linked BBS Report", type="primary"):
            bbs_data = []
            
            for beam in selected_beams:
                length_mm = st.session_state["beam_lengths"].get(beam, 0.0) # Default to 0 if missing in framing
                rebars = st.session_state["beam_rebars"].get(beam, [])
                
                for bar in rebars:
                    bbs_data.append({
                        'Member / Beam': beam,
                        'Bar Callout': bar['callout'],
                        'Diameter (mm)': bar['dia'],
                        'No. of Bars': bar['count'],
                        'Single Cut Length (mm) [From Framing]': round(length_mm, 2),
                        'Total Length (m)': round((length_mm * bar['count']) / 1000, 2),
                        'Total Weight (kg)': round(((bar['dia']**2)/162) * ((length_mm * bar['count'])/1000), 2)
                    })
            
            df_bbs = pd.DataFrame(bbs_data)
            
            if not df_bbs.empty:
                st.success("Report generated successfully!")
                st.dataframe(df_bbs, use_container_width=True)
                
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df_bbs.to_excel(writer, sheet_name='Linked BBS', index=False)
                
                st.download_button("📥 Download Linked Excel Report", data=buffer.getvalue(), file_name="Linked_BBS_Report.xlsx")
            else:
                st.warning("No linked data could be generated for the selected beams.")
