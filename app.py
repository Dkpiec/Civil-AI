import streamlit as st
import ezdxf
from ezdxf.path import make_path
import pandas as pd
import re
import io

# --- Page Configuration ---
st.set_page_config(page_title="Civil AI | BBS & Quantities", page_icon="🏗️", layout="centered")

st.title("🏗️ Civil Engineering AI Quantities")
st.markdown("Upload a foundation, column, or framing plan **DXF** to auto-generate a Bar Bending Schedule and concrete quantities.")

# --- File Uploader ---
uploaded_file = st.file_uploader("Upload CAD File (.dxf)", type=["dxf"])

if uploaded_file is not None:
    st.success(f"File '{uploaded_file.name}' loaded successfully!")
    
    with st.spinner("Analyzing CAD geometries and extracting text..."):
        # 1. Read the DXF directly from the uploaded byte stream
        try:
            with open("temp.dxf", "wb") as f:
                f.write(uploaded_file.getbuffer())
            doc = ezdxf.readfile("temp.dxf")
            msp = doc.modelspace()
        except Exception as e:
            st.error(f"Error reading DXF: {e}")
            st.stop()
        
        rebar_texts = []
        shapes = []
        
        # 2. Extract Geometry and Text
        for entity in msp.query('TEXT MTEXT'):
            text = entity.dxf.text.strip().upper() if hasattr(entity.dxf, 'text') else entity.text.strip().upper()
            if text: 
                rebar_texts.append({"raw_text": text, "layer": entity.dxf.layer})
                
        for polyline in msp.query('LWPOLYLINE'):
            if polyline.is_closed:
                # Safely calculate perimeter using ezdxf.path instead of line.length
                try:
                    p = make_path(polyline)
                    perimeter_m = p.length() / 1000.0
                except Exception:
                    perimeter_m = 0.0
                    
                # Safely calculate area
                try:
                    area_sqm = polyline.area / 1000000.0
                except Exception:
                    area_sqm = 0.0
                    
                shapes.append({
                    "layer": polyline.dxf.layer,
                    "perimeter_m": perimeter_m,
                    "area_sqm": area_sqm
                })
                
        # 3. Parse Rebar Data
        parsed_rebars = []
        for item in rebar_texts:
            match = re.search(r'(\d+)\s*[-#T]\s*(\d{2})', item["raw_text"])
            if match:
                parsed_rebars.append({
                    "element": item["layer"],
                    "count": int(match.group(1)),
                    "dia": int(match.group(2))
                })
                
        # 4. Math Engine (Standard Unit Weight & 0.45m Depth assumed)
        bbs_data = []
        for bar in parsed_rebars:
            dia, count = bar["dia"], bar["count"]
            unit_weight = (dia ** 2) / 162.0
            cutting_length = 3.0 + (2 * (9 * dia / 1000.0)) # 3m base + 90-degree hooks
            total_weight = (cutting_length * count) * unit_weight
            
            bbs_data.append({
                "Element/Layer": bar["element"],
                "Bar Dia (mm)": dia,
                "No. of Bars": count,
                "Total Weight (kg)": round(total_weight, 2)
            })
            
        df_bbs = pd.DataFrame(bbs_data)
        concrete_vol = sum([s["area_sqm"] * 0.45 for s in shapes])
        shuttering_area = sum([s["perimeter_m"] * 0.45 for s in shapes])

    # --- Display Results ---
    st.divider()
    st.subheader("📊 Estimated Quantities")
    col1, col2, col3 = st.columns(3)
    col1.metric("Concrete Volume", f"{concrete_vol:.2f} m³")
    col2.metric("Shuttering Area", f"{shuttering_area:.2f} m²")
    col3.metric("Total Steel Weight", f"{df_bbs['Total Weight (kg)'].sum():.2f} kg")

    st.subheader("📋 Bar Bending Schedule")
    if df_bbs.empty:
        st.warning("No rebar data could be cleanly extracted. Ensure callouts are in standard formats (e.g., 8-T16).")
    else:
        st.dataframe(df_bbs, use_container_width=True)

        # 5. Prepare Excel for Download
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df_bbs.to_excel(writer, sheet_name='BBS Data', index=False)
        
        st.download_button(
            label="📥 Download BBS as Excel",
            data=buffer.getvalue(),
            file_name="BBS_Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
