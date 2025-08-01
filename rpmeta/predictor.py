import json
import logging
from pathlib import Path

from rpmeta.config import Config, ModelBehavior
from rpmeta.constants import ModelEnum, TimeFormat
from rpmeta.dataset import InputRecord
from rpmeta.helpers import save_joblib
from rpmeta.regressor import TransformedTargetRegressor
from rpmeta.store import ModelStorage

logger = logging.getLogger(__name__)


class Predictor:
    def __init__(
        self,
        model: TransformedTargetRegressor,
        category_maps: dict[str, list[str]],
        config: Config,
    ) -> None:
        self.model = model
        self.category_maps = category_maps
        self.config = config

    @classmethod
    def load(
        cls,
        model_path: Path,
        model_name: ModelEnum,
        category_maps_path: Path,
        config: Config,
    ) -> "Predictor":
        """
        Load the model from the given path and category maps from the given path.

        Args:
            model_path: The path to the model directory
            model_name: The name of the model type
            category_maps_path: The path to the category maps file
            config: The configuration to use

        Returns:
            The loaded Predictor instance with the model and category maps
        """
        logger.info("Loading model %s from %s", model_name, model_path)
        model_storage = ModelStorage(model_name)
        model = model_storage.get_model(model_path)

        logger.info("Model loaded successfully, now loading category maps")

        if config.model.mmapped:
            logger.info(f"Model is at {model}")
            logger.info(f"Model regressor is at {model.regressor}")
            logger.info("Using memory-mapped model to save memory")
            model.memory_mapped_regressor()
            logger.info("Memory-mapped model created successfully")
            logger.info(f"Model is now at {model}")
            logger.info(f"Model regressor is now at {model.regressor}")


        logger.info("Loading category maps from %s", category_maps_path)
        with open(category_maps_path) as f:
            category_maps = json.load(f)

        return cls(model, category_maps, config)

    def predict(self, input_data: InputRecord, behavior: ModelBehavior) -> int:
        """
        Make prediction on the input data using the model and category maps.

        Args:
            input_data: The input data to make prediction on
            behavior: The model behavior configuration

        Returns:
            The prediction time in minutes by default
        """
        if input_data.package_name not in self.category_maps["package_name"]:
            logger.error(
                f"Package name {input_data.package_name} is not known. "
                "Please retrain the model with the new package name.",
            )
            return -1

        df = input_data.to_data_frame(self.category_maps)
        pred = self.model.predict(df)
        minutes = int(pred[0].item())

        if behavior.time_format == TimeFormat.SECONDS:
            return minutes * 60
        if behavior.time_format == TimeFormat.MINUTES:
            return minutes
        if behavior.time_format == TimeFormat.HOURS:
            return minutes // 60

        logger.error(
            f"Unknown time format {behavior.time_format}. Returning minutes as default.",
        )
        return minutes

    def save(
        self,
        result_dir: Path,
        model_name: str = "model",
        category_maps_name: str = "category_maps",
    ) -> None:
        """
        Save the model and category maps to the given directory.

        Args:
            result_dir: The directory to save the model and category maps
            model_name: The name of the model file
            category_maps_name: The name of the category maps file
        """
        logger.info("Saving predictor to %s", result_dir)

        cat_file = result_dir / f"{category_maps_name}.json"
        if cat_file.exists():
            raise ValueError(f"File {cat_file} already exists, won't overwrite it")

        save_joblib(self.model, result_dir, model_name)

        logger.info("Saving %d category maps to %s", len(self.category_maps), cat_file)
        with open(cat_file, "w") as f:
            json.dump(self.category_maps, f, indent=4)
            logger.info("Saved category maps to %s", cat_file)
