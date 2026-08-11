"""兼容入口：已迁至 script_spec.editor。"""
from script_spec.editor import SpecEditor, main  # noqa: F401

__all__ = ["SpecEditor", "main"]

if __name__ == "__main__":
    main()
