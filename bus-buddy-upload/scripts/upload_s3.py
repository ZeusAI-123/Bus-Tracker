"""Uploads the bus position files to S3 so Zetaris can read them (Zetaris can't read files on a laptop).

  pip install boto3
  AWS_PROFILE=<your profile> python scripts/upload_s3.py --bucket <bucket-name> [--prefix busbuddy/vehicle_positions/]

Then register s3://<bucket>/<prefix> in Zetaris as a Parquet source named busbuddy_s3.vehicle_positions.
"""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="busbuddy/vehicle_positions/")
    a = ap.parse_args()
    import boto3
    s3 = boto3.client("s3")
    files = sorted((ROOT / "data" / "sample" / "vehicle_positions").glob("*.parquet"))
    for f in files:
        key = a.prefix + f.name
        s3.upload_file(str(f), a.bucket, key)
        print(f"uploaded s3://{a.bucket}/{key}")
    print(f"{len(files)} files uploaded")


if __name__ == "__main__":
    main()
