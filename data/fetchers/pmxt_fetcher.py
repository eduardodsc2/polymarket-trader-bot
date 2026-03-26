"""
pmxt archive downloader — free hourly Parquet orderbook snapshots.

Archive: https://archive.pmxt.dev

Status: stub — to be implemented in Phase 1.
"""
from __future__ import annotations


class PmxtDownloader:
    BASE_URL = "https://archive.pmxt.dev"

    def download_range(self, start: str, end: str, output_dir: str = "data/raw/pmxt/") -> None:
        raise NotImplementedError("PmxtDownloader will be implemented in Phase 1")

    def load_to_db(self, parquet_dir: str = "data/raw/pmxt/") -> None:
        raise NotImplementedError("PmxtDownloader will be implemented in Phase 1")
