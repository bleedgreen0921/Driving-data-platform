import copy

import pytest

from driving_data_platform.warehouse.catalog_contract import check_schema


def schema():
    return {
        "type": "struct",
        "fields": [
            {"name": "count", "type": "long", "nullable": False, "metadata": {}},
            {
                "name": "labels",
                "type": {
                    "type": "array",
                    "elementType": "string",
                    "containsNull": True,
                },
                "nullable": True,
                "metadata": {},
            },
        ],
    }


def test_catalog_normalizes_nullability_but_requires_value_check():
    expected = schema()
    actual = copy.deepcopy(expected)
    actual["fields"][0]["nullable"] = True
    assert check_schema(actual, expected) == ["count"]


@pytest.mark.parametrize("change", ["name", "type", "order"])
def test_catalog_schema_mismatch_rejected(change):
    expected = schema()
    actual = copy.deepcopy(expected)
    if change == "name":
        actual["fields"][0]["name"] = "different"
    elif change == "type":
        actual["fields"][0]["type"] = "double"
    else:
        actual["fields"].reverse()
    with pytest.raises(ValueError):
        check_schema(actual, expected)


def test_array_element_contract_not_silently_relaxed():
    expected = schema()
    actual = copy.deepcopy(expected)
    actual["fields"][1]["type"]["containsNull"] = False
    with pytest.raises(ValueError):
        check_schema(actual, expected)
