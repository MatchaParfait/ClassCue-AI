"""Prepare official FireRed AED + VAD using the shared verified downloader."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def prepare_model(progress=None):
    from scripts.prepare_asr_modelscope import prepare_model as prepare

    prepare("fireredasr2-aed", progress)


def main():
    prepare_model()


if __name__ == "__main__":
    main()
