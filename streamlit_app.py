"""Entry point for Streamlit Community Cloud. Locally you can also run: streamlit run recon/ui.py"""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).parent / "recon" / "ui.py"), run_name="__main__")
