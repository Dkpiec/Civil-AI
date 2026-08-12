import streamlit as st
import ezdxf
import pandas as pd
import math
import re
import io
import requests
import os
from google import genai

# --- API Keys (Set these in Streamlit Cloud Secrets) ---
CONVERT_API_SECRET = os.getenv("CONVERT_API_SECRET", "YOUR_CONVERT_API_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

def convert_dwg_to_dxf(file_bytes):
    """Sends DWG to ConvertAPI and returns DXF bytes."""
    url = f"https://v2.convertapi.com/convert/dwg/to/dxf?Secret={CONVERT_API_SECRET}"
    files = {'file': ('uploaded.dwg', file_bytes)}
    response = requests.post(url, files=files)
    
    if response.status_code == 200:
        dxf_url = response.json()['Files'][0]['Url']
        dxf_response = requests.get(dxf_url)
        return dxf_response.content
    else:
        st.error(f"DWG Conversion Failed: {response.text}")
        return None

def ask_llm_fallback(rebar_texts, lines_summary):
    """Sends unstructured CAD data to Gemini if strict math fails."""
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        return None
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are a structural engineering AI. Match the rebar callouts to the nearest line lengths based on typical CAD layout logic.
    Rebar Texts: {rebar_texts[:20]}
    Line Summaries: {lines_summary[:20]}
    
    Output a JSON array of objects with 'Callout', 'Matched_Length_mm', 'Diameter', and 'Count'.
    """
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        return response.text
    except Exception as e:
        st.error(f"LLM Fallback error: {e}")
        return None

# --- UI Setup ---
st.set_page_config(page_title="Universal Civil AI", page_icon="🏗️", layout="wide")
st.title("🏗️ Universal Civil AI: Auto-Detect & Convert")

# Standard project metadata input
project_name = st.text_input("Project Description", placeholder="e.g., 26x60 Plot Foundation Plan")

uploaded_file = st.file_uploader("Upload CAD Drawing (.dxf or .dwg)", type=[".dxf", ".dwg"])

if uploaded_file is not None:
    file_ext = uploaded_file.name.split('.')[-1].lower()
    file_bytes = uploaded_file.read()
    
    with st.spinner(f"Processing {file_ext.upper()} file..."):
        # 1. Automatic DWG to DXF Conversion
        if file_ext == 'dwg':
            st.info("DWG detected. Converting to DXF via Cloud API...")
            dxf_bytes = convert_dwg_to_dxf(file_bytes)
            if not dxf_bytes:
                st.stop()
        else:
            dxf_bytes = file_bytes

        # Save to temp file for ezdxf
        with open("temp.dxf", "wb") as f:
            f.write(dxf_bytes)
            
        doc = ezdxf.readfile("temp.dxf")
        msp = doc.modelspace()

        # 2. Dynamic Text Recognition & Layer Inference
        rebar_texts = []
        inferred_rebar_layers = set()
        
        for text in msp.query('TEXT MTEXT'):
            content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip().upper()
            
            try: insert_pt = (text.dxf.insert.x, text.dxf.insert.y)
            except AttributeError:
                try: insert_pt = (text.dxf.align_point.x, text.dxf.align_point.y)
                except AttributeError: continue

            # Dynamic Regex: Matches 8-T16, 12#16, Y20, T10
            if re.search(r'(\d+)?\s*[-#T]?\s*([T|Y|D|\#]?\d{2})', content):
                layer = text.dxf.layer
                rebar_texts.append({'content': content, 'pos': insert_pt, 'layer': layer})
                
                # LAYER INFERENCE: If we found rebar text here, assume this layer holds rebar!
                inferred_rebar_layers.add(layer)

        st.write(f"🔍 **Auto-Detected Rebar Layers:** {', '.join(inferred_rebar_layers) if inferred_rebar_layers else 'None found'}")

        # 3. Filter Geometry strictly by Inferred Layers
        lines_summary = []
        for entity in msp.query('LINE LWPOLYLINE'):
            if entity.dxf.layer in inferred_rebar_layers:
                # Calculate lengths as done in the previous script (placeholder here)
                length_mm = 5000 # Example placeholder for length math
                lines_summary.append({'layer': entity.dxf.layer, 'length': length_mm})

        # 4. Standard Geometry Matching (Placeholder for your math engine)
        bbs_data = [] 
        # (Insert your distance/midpoint matching logic here)
        
        # 5. The LLM Fallback Mechanism
        if not bbs_data and len(rebar_texts) > 0 and len(lines_summary) > 0:
            st.warning("⚠️ Standard geometry math failed to map bars to text. Engaging Gemini LLM Fallback...")
            
            llm_result = ask_llm_fallback(rebar_texts, lines_summary)
            
            if llm_result:
                st.success("🤖 LLM successfully recovered unstructured data!")
                # Parse LLM JSON to df_bbs
                df_bbs = pd.read_json(io.StringIO(llm_result))
            else:
                st.error("LLM fallback also failed. Please check drawing clarity.")
                df_bbs = pd.DataFrame()
        else:
            df_bbs = pd.DataFrame(bbs_data)

        if not df_bbs.empty:
            st.dataframe(df_bbs)
