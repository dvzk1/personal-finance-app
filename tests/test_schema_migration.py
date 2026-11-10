import pytest

from finance import sheets

# Testam a implementação do Google, qualquer que seja o backend da execução.
pytestmark = pytest.mark.usefixtures("google_backend")


class FakeWorksheet:
    def __init__(self, title, header):
        self.title = title
        self.header = list(header)
        self.col_count = len(header)

    def row_values(self, row):
        return list(self.header) if row == 1 else []

    def resize(self, rows=None, cols=None):
        if cols is not None:
            self.col_count = cols

    def update(self, values, range_name, value_input_option):
        self.header.extend(values[0])


class FakeSheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def worksheets(self):
        return list(self._worksheets)

    def add_worksheet(self, title, rows, cols):
        worksheet = FakeWorksheet(title, [])
        worksheet.col_count = cols
        self._worksheets.append(worksheet)
        return worksheet


def test_schema_migration_adds_missing_fields(monkeypatch):
    worksheets = [
        FakeWorksheet(tab, [name for name, _ in schema]
                      if tab != "Ledger"
                      else [name for name, _ in schema][:-1])
        for tab, schema in sheets.SCHEMAS.items()
        if tab != "SubcategoryMeta"
    ]
    worksheets.append(FakeWorksheet("Config", ["key", "value"]))
    fake_sheet = FakeSheet(worksheets)
    config = {"schema_version": 1}
    sheets.open_sheet = lambda: fake_sheet
    # a implementação do Google chama as próprias funções por sheets.GOOGLE
    monkeypatch.setitem(sheets.GOOGLE, "read_config",
                        lambda key, default=None: config.get(key, default))
    monkeypatch.setitem(sheets.GOOGLE, "write_config",
                        lambda key, value: config.__setitem__(key, value))
    monkeypatch.setitem(sheets.GOOGLE, "reset_cache", lambda: None)

    changes = sheets.ensure_current_schema()

    ledger = next(worksheet for worksheet in worksheets
                  if worksheet.title == "Ledger")
    subcategory_meta = next(worksheet for worksheet in worksheets
                            if worksheet.title == "SubcategoryMeta")
    assert ledger.header == [name for name, _ in sheets.LEDGER_SCHEMA]
    assert subcategory_meta.header == [name for name, _ in sheets.SUBCATEGORY_META_SCHEMA]
    last_ledger_column = sheets.LEDGER_SCHEMA[-1][0]
    assert changes == [f"coluna Ledger.{last_ledger_column}", "aba SubcategoryMeta",
                       "Config.timezone"]
    assert config["timezone"] == "America/Sao_Paulo"
    assert config["schema_version"] == sheets.SCHEMA_VERSION
    assert sheets.ensure_current_schema() == []


def test_google_implementation_never_falls_into_the_local_backend(local_db, monkeypatch):
    """No modo local os nomes do módulo apontam para o SQLite. A migração do Google tem
    de gravar na planilha, não no arquivo local."""
    worksheets = [FakeWorksheet(tab, [name for name, _ in schema])
                  for tab, schema in sheets.SCHEMAS.items()]
    worksheets.append(FakeWorksheet("Config", ["key", "value"]))
    config = {}
    monkeypatch.setattr(sheets, "open_sheet", lambda: FakeSheet(worksheets))
    monkeypatch.setitem(sheets.GOOGLE, "read_config",
                        lambda key, default=None: config.get(key, default))
    monkeypatch.setitem(sheets.GOOGLE, "write_config",
                        lambda key, value: config.__setitem__(key, value))
    monkeypatch.setitem(sheets.GOOGLE, "reset_cache", lambda: None)

    sheets.GOOGLE["ensure_current_schema"]()

    assert config["schema_version"] == sheets.SCHEMA_VERSION
    assert sheets.read_config("schema_version") is None, "o banco local não foi tocado"
