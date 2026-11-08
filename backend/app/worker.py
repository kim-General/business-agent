"""One compiled-plan query per process. This process never receives model credentials."""
import json
import sys
from pathlib import Path


def main():
    from app.query import QueryEngine
    from app.schemas import AnalysisPlan
    from app.warehouse import Warehouse

    request = json.load(sys.stdin)
    try:
        warehouse = Warehouse(Path(request["data_dir"]), Path(request["source_root"]))
        result = QueryEngine(warehouse).execute(AnalysisPlan.model_validate(request["plan"]))
        print(json.dumps({"ok":True,"result":result},ensure_ascii=False,allow_nan=False,default=str))
    except Exception as exc:
        print(json.dumps({"ok":False,"error":str(exc)},ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
