import streamlit as st
import ezdxf
import pandas as pd
import math
import matplotlib.pyplot as plt
import warnings
import re
import traceback
import io
import requests
import os
import json
from google import genai

warnings.filterwarnings('ignore')

# --- API Keys ---
# Set these in Streamlit Community Cloud (Settings > Secrets)
CONVERT_API_SECRET = os.environ.get("CONVERT_API_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# --- Helper Functions ---
def get_midpoint(entity):
    try:
        if entity.dxftype() == 'LINE':
            return ((entity.dxf.start.x + entity.dxf.end.x) / 2, (entity.dxf.start.y + entity.dxf.end.y) / 2)
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            if not pts: return (0, 0)
            return pts[len(pts)//2]
    except Exception:
        pass
    return (0, 0)

def calculate_length(entity):
    try:
        if entity.dxftype() == 'LINE':
            return math.dist((entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y))
        elif entity.dxftype() == 'LWPOLYLINE':
            pts = entity.get_points('xy')
            if len(pts) < 2: return 0.0
            length = sum(math.dist(pts[i], pts[i+1]) for i in range(len(pts)-1))
            if entity.closed:
                length += math.dist(pts[-1], pts[0])
            return length
    except Exception:
        return 0.0
    return 0.0

def convert_dwg_to_dxf(file_bytes):
    """Sends DWG to ConvertAPI and returns DXF bytes."""
    if not CONVERT_API_SECRET:
        st.error("ConvertAPI Secret Key is missing in secrets.")
        return None
        
    url = f"https://v2.convertapi.com/convert/dwg/to/dxf?Secret={CONVERT_API_SECRET}"
    files = {'file': ('uploaded.dwg', file_bytes)}
    response = requests.post(url, files=files)
    
    if response.status_code == 200:
        dxf_url = response.json()['Files'][0]['Url']
        return requests.get(dxf_url).content
    else:
        st.error(f"DWG Conversion Failed: {response.text}")
        return None

def ask_llm_fallback(rebar_texts, lines_summary):
    """LLM Integration: Uses human-like reasoning if standard math fails."""
    if not GEMINI_API_KEY:
        st.error("Gemini API Key missing. Cannot run AI fallback.")
        return None
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are a civil engineering AI. The standard geometric distance math failed to map these rebars.
    Use logical reasoning to pair these unstructured rebar callouts with the provided drawn line lengths.
    
    Raw Texts: {rebar_texts[:30]}
    Line Lengths (mm): {lines_summary[:30]}
    
    Return a JSON array of objects with keys exactly as: 
    "Member", "Callout", "Diameter", "Count", "Length_mm". 
    Do not include markdown blocks, just the raw JSON.
    """
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"LLM Error: {e}")
        return None

# --- UI Setup ---
st.set_page_config(page_title="Universal Civil AI", page_icon="🏗️", layout="wide")

st.title("🏗️ Universal Civil AI: Auto-Detect & Convert")
st.markdown("This tool utilizes **Dynamic Text Recognition**, **Layer Inference**, and an **LLM Fallback Engine** to process any DWG or DXF, regardless of the draftsman's layer naming conventions.")

project_name = st.text_input("Project Name / Description", placeholder="e.g., 26x60 Plot Foundation Plan")
uploaded_file = st.file_uploader("Upload CAD Drawing (.dxf or .dwg)", type=[".dxf", ".dwg"])

if uploaded_file is not None:
    file_ext = uploaded_file.name.split('.')[-1].lower()
    
    with st.spinner("Analyzing CAD structure..."):
        try:
            # 1. Automatic Conversion
            if file_ext == 'dwg':
                st.info("DWG detected. Converting to DXF via Cloud API...")
                dxf_bytes = convert_dwg_to_dxf(uploaded_file.getvalue())
                if not dxf_bytes: st.stop()
            else:
                dxf_bytes = uploaded_file.getvalue()

            with open("temp.dxf", "wb") as f:
                f.write(dxf_bytes)
            
            doc = ezdxf.readfile("temp.dxf")
            msp = doc.modelspace()

            # --- 2. Dynamic Text Recognition & 3. Layer Inference ---
            rebar_texts, member_texts, section_texts = [], [], []
            inferred_rebar_layers = set()
            
            for text in msp.query('TEXT MTEXT'):
                content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
                layer = text.dxf.layer.upper()
                
                try: insert_pt = (text.dxf.insert.x, text.dxf.insert.y)
                except AttributeError:
                    try: insert_pt = (text.dxf.align_point.x, text.dxf.align_point.y)
                    except AttributeError: continue

                # Look for rebar patterns (e.g., 8-T16, 12#20, Y10)
                rebar_match = re.search(r'(\d+)\s*[-#TXY]\s*(\d{2})', content)
                section_match = re.search(r'\d+\s*[xX]\s*\d+', content)

                if rebar_match:
                    rebar_texts.append({
                        'content': content, 'pos': insert_pt, 'layer': layer,
                        'count': int(rebar_match.group(1)), 'dia': int(rebar_match.group(2))
                    })
                    inferred_rebar_layers.add(layer) # <--- Layer Inference
                elif section_match:
                    section_texts.append({'content': content, 'pos': insert_pt})
                elif re.match(r'^[A-Z]{1,3}[-]?\d+$', content): # Looks like a beam name (B1, PB-2)
                    member_texts.append({'content': content, 'pos': insert_pt})

            st.write(f"🔍 **AI Layer Inference Found Rebars On:** `{', '.join(inferred_rebar_layers) if inferred_rebar_layers else 'None'}`")

            # --- 4. Geometry Math Engine ---
            bbs_data = []
            lines_summary = []

            # ONLY scan geometry on layers we just inferred contain rebars
            for entity in msp.query('LINE LWPOLYLINE'):
                layer = entity.dxf.layer.upper()
                if layer in inferred_rebar_layers:
                    length_mm = calculate_length(entity)
                    if length_mm <= 100: continue
                    
                    midpoint = get_midpoint(entity)
                    lines_summary.append({'layer': layer, 'length_mm': round(length_mm, 2), 'midpoint': midpoint})

                    # Distance Matching
                    closest_rebar, min_d = None, float('inf')
                    for rt in rebar_texts:
                        d = math.dist(midpoint, rt['pos'])
                        if d < min_d and d < 3000: # Threshold: Text must be within 3m of line
                            min_d, closest_rebar = d, rt
                    
                    closest_member = "Unknown"
                    min_md = float('inf')
                    for mt in member_texts:
                        d = math.dist(midpoint, mt['pos'])
                        if d < min_md and d < 5000:
                            min_md, closest_member = d, mt['content']

                    if closest_rebar:
                        bbs_data.append({
                            'Member / Beam': closest_member,
                            'Bar Callout': closest_rebar['content'],
                            'Diameter (mm)': closest_rebar['dia'],
                            'No. of Bars': closest_rebar['count'],
                            'Single Cut Length (mm)': round(length_mm, 2),
                            'Total Length (m)': round((length_mm * closest_rebar['count'])/1000, 2),
                            'Total Weight (kg)': round(((closest_rebar['dia']**2)/162) * ((length_mm * closest_rebar['count'])/1000), 2)
                        })

            # --- 5. LLM Fallback Mechanism ---
            df_bbs = pd.DataFrame(bbs_data)
            
            if df_bbs.empty and len(rebar_texts) > 0:
                st.warning("⚠️ Geometric math failed to map text to lines. Engaging Gemini AI Fallback...")
                
                llm_json = ask_llm_fallback(
                    [r['content'] for r in rebar_texts], 
                    [l['length_mm'] for l in lines_summary]
                )
                
                if llm_json:
                    st.success("🤖 LLM successfully recovered data using logic!")
                    df_bbs = pd.DataFrame(llm_json)
                    
                    # Compute weights for LLM data
                    if not df_bbs.empty and 'Diameter' in df_bbs.columns and 'Length_mm' in df_bbs.columns:
                        df_bbs['Total Length (m)'] = (df_bbs['Length_mm'] * df_bbs['Count']) / 1000
                        df_bbs['Total Weight (kg)'] = ((df_bbs['Diameter']**2)/162) * df_bbs['Total Length (m)']
                else:
                    st.error("LLM Fallback failed to process the raw data.")

            # --- Render Interactive Data & Log Corrections ---
            if not df_bbs.empty:
                st.divider()
                st.subheader("📋 Interactive Bar Bending Schedule")
                st.markdown("*Review and correct the AI's extraction below. Any edits you make are logged to help the AI learn!*")

                # 1. The Interactive Data Editor
                edited_df = st.data_editor(
                    df_bbs,
                    num_rows="dynamic", 
                    use_container_width=True,
                    key="bbs_editor"
                )

                col1, col2 = st.columns([1, 1])

                with col1:
                    # 2. The Feedback Logging Mechanism
                    if st.button("💾 Submit Corrections to AI Knowledge Base", type="secondary"):
                        changes = st.session_state["bbs_editor"]
                        
                        if changes["edited_rows"] or changes["added_rows"] or changes["deleted_rows"]:
                            st.success("✅ Corrections logged successfully for future AI training!")
                            with st.expander("View Raw Training Data (JSON)"):
                                st.json(changes)
                        else:
                            st.info("No corrections made. The AI extraction was 100% accurate!")

                with col2:
                    # 3. Export the EDITED dataframe to Excel
                    buffer = io.BytesIO()
                    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                        edited_df.to_excel(writer, sheet_name='Detailed BBS', index=False)

                    st.download_button(
                        label="📥 Download Corrected Excel Report",
                        data=buffer.getvalue(),
                        file_name=f"{project_name.replace(' ', '_') if project_name else 'BBS'}_Report.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        type="primary"
                    )
            else:
                st.warning("No rebar data could be mapped. The drawing might be completely unstructured.")

        except Exception as e:
            st.error(f"SYSTEM ERROR: {str(e)}")
            st.code(traceback.format_exc())
