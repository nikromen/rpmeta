import json
from pathlib import Path

import requests

from test.helpers import run_rpmeta_cli


def test_train(model_and_types):
    trained_model_file, cat_dtypes_file, model_name = model_and_types
    assert trained_model_file.exists(), f"{model_name} model was not saved"
    assert trained_model_file.stat().st_size > 0, f"{model_name} model is empty"
    assert cat_dtypes_file.exists(), "Category dtypes file was not saved"
    assert cat_dtypes_file.stat().st_size > 0, "Category dtypes file is empty"
    assert cat_dtypes_file.suffix == ".json", "Category dtypes file is not a JSON file"


def test_predict(model_and_types):
    trained_model_file, cat_dtypes_file, model_name = model_and_types
    dataset_path = Path(__file__).parent.parent / "data" / "dataset_predict.json"
    cmd = [
        "run",
        "--model-dir",
        str(trained_model_file),
        "--model-name",
        model_name,
        "--categories",
        str(cat_dtypes_file),
        "predict",
        "--data",
        str(dataset_path),
    ]

    result = run_rpmeta_cli([*cmd, "--output-type", "json"])

    assert result.returncode == 0

    response = json.loads(result.stdout)
    assert len(response) == 1
    assert isinstance(response["prediction"], int)

    result = run_rpmeta_cli([*cmd, "--output-type", "text"])
    assert result.returncode == 0
    # check if output is Prediction: <number>
    assert result.stdout.startswith("Prediction: ")
    assert result.stdout.split(":")[1].strip().isnumeric()


def test_api_server(api_server):
    response = requests.post("http://localhost:9876")
    assert response.status_code == 404
    assert "Not Found" in response.text

    response = requests.post(
        "http://localhost:9876/predict",
        json=json.loads(
            (Path(__file__).parent.parent / "data" / "dataset_predict.json").read_text(),
        ),
    )
    assert response.status_code == 200
    assert "prediction" in response.json()
    assert isinstance(response.json()["prediction"], int)

    bad_response = requests.post(
        "http://localhost:9876/predict",
        json={"foo": "bar"},
    )
    assert bad_response.status_code == 422
    assert "detail" in bad_response.json()
