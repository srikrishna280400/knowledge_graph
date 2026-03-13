from .db import engine, Base
from . import models  # noqa: F401

def main():
    Base.metadata.create_all(bind=engine)
    print("OK: created data/kg.sqlite (if missing) and tables")

if __name__ == "__main__":
    main()
