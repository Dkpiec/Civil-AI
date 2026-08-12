import streamlit as st
import ezdxf
import pandas as pd
import math
import matplotlib.pyplot as plt
import warnings
import re
import traceback
import io

warnings.filterwarnings('ignore')

# --- Helper Functions ---
def get_midpoint(entity):
    try:
        if entity.dxftype() == 'LINE':
            return ((entity.dxf.start.x + entity.dxf.end.x) / 2,
                    (entity.dxf.start.y + entity.dxf.end.y) / 2)
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

def parse_callout(text):
    try:
        matches = re.findall(r'(\d+)\s*-\s*[T]?(\d+)', str(text).upper())
        results = []
        for match in matches:
            results.append({'count': int(match[0]), 'diameter': int(match[1])})
        return results
    except Exception:
        return []

# --- Page Configuration ---
st.set_page_config(page_title="Ultimate Excel BBS Generator", page_icon="🏗️", layout="wide")

st.title("🏗️ Ultimate Excel BBS Generator")
st.markdown("This version groups your rebars by Beam Name, separates them with blank rows, and exports a single `.xlsx` file containing multiple sheets.")

# --- UI Setup ---
uploaded_file = st.file_uploader("Upload CAD Drawing (.dxf)", type=[".dxf"])

if uploaded_file is not None:
    with st.spinner("Processing DXF geometries and matching text..."):
        try:
            # 0. Save the uploaded file to a temporary location for ezdxf to read
            with open("temp.dxf", "wb") as f:
                f.write(uploaded_file.getbuffer())
                
            doc = ezdxf.readfile("temp.dxf")
            msp = doc.modelspace()

            # 1. Extract and Categorize all Text
            rebar_texts = []
            member_texts = []
            section_texts = []

            for text in msp.query('TEXT MTEXT'):
                layer = text.dxf.layer.upper()
                content = str(text.dxf.text if text.dxftype() == 'TEXT' else text.text).strip()

                try: insert_pt = (text.dxf.insert.x, text.dxf.insert.y)
                except AttributeError:
                    try: insert_pt = (text.dxf.align_point.x, text.dxf.align_point.y)
                    except AttributeError: continue

                if layer == 'CBM_TEXT':
                    rebar_texts.append({'content': content, 'pos': insert_pt})
                elif layer == 'CBM_TEXT2':
                    if 'X' in content.upper() and any(c.isdigit() for c in content):
                        section_texts.append({'content': content, 'pos': insert_pt})
                    else:
                        member_texts.append({'content': content, 'pos': insert_pt})

            bbs_data = []

            # 2. Match Geometry to Texts
            for entity in msp.query('LINE LWPOLYLINE'):
                layer = entity.dxf.layer.upper()
                if 'REINF' in layer or 'LINKS' in layer or 'SW' in layer:
                    length_mm = calculate_length(entity)
                    if length_mm <= 0: continue
                    midpoint = get_midpoint(entity)

                    def get_closest(target_pos, text_list):
                        closest, min_d = None, float('inf')
                        for t in text_list:
                            d = math.dist(target_pos, t['pos'])
                            if d < min_d and d < 5000:
                                min_d, closest = d, t['content']
                        return closest

                    closest_callout = get_closest(midpoint, rebar_texts)
                    closest_member = get_closest(midpoint, member_texts) or 'Unknown'
                    closest_section = get_closest(midpoint, section_texts) or 'Unknown'

                    if closest_callout:
                        parsed_bars = parse_callout(closest_callout)
                        for bar in parsed_bars:
                            bbs_data.append({
                                'Member / Beam': closest_member,
                                'Concrete Section': closest_section,
                                'Rebar Layer': layer,
                                'Bar Callout': closest_callout,
                                'Diameter (mm)': bar['diameter'],
                                'No. of Bars': bar['count'],
                                'Single Cut Length (mm)': round(length_mm, 2),
                                'Total Length (m)': round((length_mm * bar['count'])/1000, 2),
                                'Total Weight (kg)': round(((bar['diameter']**2)/162) * ((length_mm * bar['count'])/1000), 2)
                            })

            df_bbs = pd.DataFrame(bbs_data)
            
            if df_bbs.empty:
                st.warning("Could not map rebars. Make sure your layers ('CBM_TEXT', 'CBM_TEXT2', 'REINF') match the script logic.")
                st.stop()

            # --- Sort and Group Beams with Blank Rows ---
            df_bbs = df_bbs.sort_values(by=['Member / Beam', 'Concrete Section']).reset_index(drop=True)

            spaced_data = []
            prev_beam = None

            for idx, row in df_bbs.iterrows():
                current_beam = row['Member / Beam']
                if prev_beam is not None and current_beam != prev_beam:
                    empty_row = pd.Series([None] * len(df_bbs.columns), index=df_bbs.columns)
                    spaced_data.append(empty_row)

                spaced_data.append(row)
                prev_beam = current_beam

            df_spaced_bbs = pd.DataFrame(spaced_data)

            # 3. Calculate Concrete Quantities
            concrete_data = []
            for (member, section), group in df_bbs.groupby(['Member / Beam', 'Concrete Section']):
                w, d = 0, 0
                match = re.search(r'(\d+)\s*[xX]\s*(\d+)', str(section))
                if match:
                    w, d = int(match.group(1)), int(match.group(2))

                est_span_m = group['Single Cut Length (mm)'].max() / 1000
                vol_m3 = round((w/1000) * (d/1000) * est_span_m, 3) if w > 0 else 0.0

                concrete_data.append({
                    'Member / Beam': member,
                    'Cross Section': section,
                    'Width (mm)': w,
                    'Depth (mm)': d,
                    'Est. Beam Span (m)': round(est_span_m, 2),
                    'Concrete Volume (m3)': vol_m3
                })

            df_concrete = pd.DataFrame(concrete_data)

            # --- Export to Single Excel File with Multiple Sheets in Memory ---
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                df_spaced_bbs.to_excel(writer, sheet_name='Detailed BBS', index=False)
                df_concrete.to_excel(writer, sheet_name='Concrete Quantities', index=False)
            
            # 4. Charting
            summary_chart = df_bbs.groupby('Diameter (mm)')['Total Weight (kg)'].sum().reset_index()
            fig, ax = plt.subplots(figsize=(8, 6))
            ax.bar(summary_chart['Diameter (mm)'].astype(str), summary_chart['Total Weight (kg)'], color='#3B82F6')
            ax.set_title("Automated Rebar Weight by Diameter", fontweight='bold')
            ax.set_ylabel("Weight (kg)")
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            plt.tight_layout()

            # --- UI Layout ---
            st.divider()
            col1, col2 = st.columns([1, 2])
            
            with col1:
                st.subheader("📥 Downloads")
                st.download_button(
                    label="Download Excel Report (.xlsx)",
                    data=buffer.getvalue(),
                    file_name="Ultimate_BBS_and_Concrete.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
                st.success("File processed successfully!")

            with col2:
                st.subheader("📊 Weight Chart")
                st.pyplot(fig)

            st.divider()
            st.subheader("📋 Detailed Data Preview (Top 50 Rows)")
            preview_df = df_spaced_bbs[['Member / Beam', 'Concrete Section', 'Bar Callout', 'Diameter (mm)', 'Total Length (m)', 'Total Weight (kg)']].head(50)
            st.dataframe(preview_df, use_container_width=True)

        except Exception as e:
            st.error(f"CRITICAL ERROR: {str(e)}")
            st.code(traceback.format_exc())
