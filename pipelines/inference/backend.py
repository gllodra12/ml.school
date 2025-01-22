import importlib
import json
import logging
import os
import random
import re
import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from metaflow import Config, Parameter


class BackendMixin:
    """A mixin for managing the backend implementation of a model.

    This mixin is designed to be combined with any pipeline that requires accessing
    a hosted model. The mixin provides a common interface for interacting with the
    model and its associated database.
    """

    backend_config = Config(
        "backend",
        help=("Backend configuration used to initialize the provided backend class."),
        default={},
    )

    backend = Parameter(
        "backend",
        help="Name of the class implementing the `backend.Backend` abstract class.",
        default="backend.Local",
    )

    def load_backend(self):
        """Instantiate the backend class using the supplied configuration."""
        try:
            module, cls = self.backend.rsplit(".", 1)
            module = importlib.import_module(module)
            backend_impl = getattr(module, cls)(config=self._get_config())
        except Exception as e:
            message = f"There was an error instantiating class {self.backend}."
            raise RuntimeError(message) from e
        else:
            logging.info("Backend: %s", self.backend)
            return backend_impl

    def _get_config(self):
        """Return the backend configuration with environment variables expanded.

        This function supports using ${ENVIRONMENT_VARIABLE} syntax as part of the
        configuration values.
        """
        if not self.backend_config:
            return None

        config = self.backend_config.to_dict()
        pattern = re.compile(r"\$\{(\w+)\}")

        def replacer(match):
            env_var = match.group(1)
            return os.getenv(env_var, f"${{{env_var}}}")

        for key, value in self.backend_config.items():
            if isinstance(value, str):
                config[key] = pattern.sub(replacer, value)

        return config


class Backend(ABC):
    """Abstract class defining the interface of a backend."""

    @abstractmethod
    def load(self, limit: int) -> pd.DataFrame | None:
        """Load production data from the backend database.

        Args:
            limit: The maximum number of samples to load from the database.

        """

    @abstractmethod
    def save(self, model_input: pd.DataFrame, model_output: list) -> None:
        """Save production data and model outputs to the database.

        Args:
            model_input: The input data received by the model
            model_output: The output data generated by the model.

        """

    @abstractmethod
    def label(self, ground_truth_quality: float = 0.8) -> int:
        """Label every unlabeled sample stored in the backend database.

        This function will generate fake ground truth data for any unlabeled samples
        stored in the backend database.

        Args:
            ground_truth_quality: The quality of the ground truth labels to generate.
                A value of 1.0 will generate labels that match the model predictions. A
                value less than 1.0 will introduce noise in the labels to simulate
                inaccurate model predictions

        """

    @abstractmethod
    def invoke(self, payload: list | dict) -> dict | None:
        """Make a prediction request to the hosted model.

        Args:
            payload: The data to send to the model for prediction.

        """

    @abstractmethod
    def deploy(self, model_uri: str, model_version: str) -> None:
        """Deploy the supplied model.

        Args:
            model_uri: The path where the model artifacts are located.
            model_version: The version of the model that will be deployed.

        """

    def get_fake_label(self, prediction, ground_truth_quality):
        """Generate a fake ground truth label for a sample.

        This function will randomly return a ground truth label taking into account the
        prediction quality we want to achieve.

        Args:
            prediction: The model prediction for the sample.
            ground_truth_quality: The quality of the ground truth labels to generate.

        """
        return (
            prediction
            if random.random() < ground_truth_quality
            else random.choice(["Adelie", "Chinstrap", "Gentoo"])
        )


class Local(Backend):
    """Local backend implementation.

    A model with this backend will be deployed using `mlflow model serve` and will use
    a SQLite database to store production data.
    """

    def __init__(self, config: dict | None = None) -> None:
        """Initialize backend using the supplied configuration.

        If the configuration is not provided, the class will attempt to read the
        configuration from environment variables.
        """
        self.target = (
            config.get("target", "http://127.0.0.1:8080/invocations")
            if config
            else "http://127.0.0.1:8080/invocations"
        )
        self.database = "penguins.db"

        if config:
            self.database = config.get("database", self.database)
        else:
            self.database = os.getenv("MODEL_BACKEND_DATABASE", self.database)

        logging.info("Backend database: %s", self.database)

    def load(self, limit: int = 100) -> pd.DataFrame | None:
        """Load production data from a SQLite database."""
        import pandas as pd

        if not Path(self.database).exists():
            logging.error("Database %s does not exist.", self.database)
            return None

        connection = sqlite3.connect(self.database)

        query = (
            "SELECT island, sex, culmen_length_mm, culmen_depth_mm, flipper_length_mm, "
            "body_mass_g, prediction, ground_truth FROM data "
            "ORDER BY date DESC LIMIT ?;"
        )

        data = pd.read_sql_query(query, connection, params=(limit,))
        connection.close()

        return data

    def save(self, model_input: pd.DataFrame, model_output: list):
        """Save production data to a SQLite database.

        If the database doesn't exist, this function will create it.
        """
        logging.info("Storing production data in the database...")

        connection = None
        try:
            connection = sqlite3.connect(self.database)

            # Let's create a copy from the model input so we can modify the DataFrame
            # before storing it in the database.
            data = model_input.copy()

            # We need to add the current date and time so we can filter data based on
            # when it was collected.
            data["date"] = datetime.now(timezone.utc)

            # Let's initialize the prediction and confidence columns with None. We'll
            # overwrite them later if the model output is not empty.
            data["prediction"] = None
            data["confidence"] = None

            # Let's also add a column to store the ground truth. This column can be
            # used by the labeling team to provide the actual species for the data.
            data["ground_truth"] = None

            # If the model output is not empty, we should update the prediction and
            # confidence columns with the corresponding values.
            if model_output is not None and len(model_output) > 0:
                data["prediction"] = [item["prediction"] for item in model_output]
                data["confidence"] = [item["confidence"] for item in model_output]

            # Let's automatically generate a unique identified for each row in the
            # DataFrame. This will be helpful later when labeling the data.
            data["uuid"] = [str(uuid.uuid4()) for _ in range(len(data))]

            # Finally, we can save the data to the database.
            data.to_sql("data", connection, if_exists="append", index=False)

        except sqlite3.Error:
            logging.exception(
                "There was an error saving production data to the database.",
            )
        finally:
            if connection:
                connection.close()

    def label(self, ground_truth_quality: float = 0.8) -> int:
        """Label every unlabeled sample stored in the backend database."""
        if not Path(self.database).exists():
            logging.error("Database %s does not exist.", self.database)
            return 0

        connection = None
        try:
            connection = sqlite3.connect(self.database)

            # We want to return any unlabeled samples from the database.
            df = pd.read_sql_query(
                "SELECT * FROM data WHERE ground_truth IS NULL",
                connection,
            )
            logging.info("Loaded %s unlabeled samples.", len(df))

            # If there are no unlabeled samples, we don't need to do anything else.
            if df.empty:
                return 0

            for _, row in df.iterrows():
                uuid = row["uuid"]
                label = self.get_fake_label(row["prediction"], ground_truth_quality)

                # Update the database
                update_query = "UPDATE data SET ground_truth = ? WHERE uuid = ?"
                connection.execute(update_query, (label, uuid))

            connection.commit()
            return len(df)
        except Exception:
            logging.exception("There was an error labeling production data")
            return 0
        finally:
            if connection:
                connection.close()

    def invoke(self, payload: list | dict) -> dict | None:
        """Make a prediction request to the hosted model."""
        import requests

        logging.info('Running prediction on "%s"...', self.target)

        try:
            predictions = requests.post(
                url=self.target,
                headers={"Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "inputs": payload,
                    },
                ),
                timeout=5,
            )
            return predictions.json()
        except Exception:
            logging.exception("There was an error sending traffic to the endpoint.")
            return None

    def deploy(self, model_uri: str, model_version: str) -> None:
        """Not Implemented.

        Deploying a model is not applicable when serving the model directly.
        """


class Sagemaker(Backend):
    """Sagemake backend implementation.

    A model with this backend will be deployed to Sagemaker and will use S3
    to store production data.
    """

    def __init__(self, config: dict | None = None) -> None:
        """Initialize backend using the supplied configuration."""
        from mlflow.deployments import get_deploy_client

        self.target = config.get("target", "penguins") if config else "penguins"
        self.data_capture_uri = config.get("data-capture-uri", None) if config else None
        self.ground_truth_uri = config.get("ground-truth-uri", None) if config else None

        # Let's make sure the ground truth uri ends with a '/'
        self.ground_truth_uri = self.ground_truth_uri.rstrip("/") + "/"

        self.assume_role = config.get("assume_role", None) if config else None
        self.region = config.get("region", "us-east-1") if config else "us-east-1"

        self.deployment_target_uri = (
            f"sagemaker:/{self.region}/{self.assume_role}"
            if self.assume_role
            else f"sagemaker:/{self.region}"
        )

        self.deployment_client = get_deploy_client(self.deployment_target_uri)

        logging.info("Target: %s", self.target)
        logging.info("Data capture URI: %s", self.data_capture_uri)
        logging.info("Ground truth URI: %s", self.ground_truth_uri)
        logging.info("Assume role: %s", self.assume_role)
        logging.info("Region: %s", self.region)
        logging.info("Deployment target URI: %s", self.deployment_target_uri)

    def load(self, limit: int = 100) -> pd.DataFrame:
        """Load production data from an S3 bucket."""
        import boto3

        s3 = boto3.client("s3")
        data = self._load_collected_data(s3)

        if data.empty:
            return data

        # We want to return samples that have a ground truth label.
        data = data[data["species"].notna()]

        # Rename `species` column to `ground_truth`.
        data = data.rename(columns={"species": "ground_truth"})

        # We need to remove a few columns that are not needed for the monitoring tests
        # and return `limit` number of samples.
        data = data.drop(columns=["date", "event_id", "confidence"])

        # We want to return `limit` number of samples.
        return data.head(limit)

    def label(self, ground_truth_quality=0.8):
        """Label every unlabeled sample stored in S3.

        This function loads any unlabeled data from the location where Sagemaker stores
        the data captured by the endpoint and generates fake ground truth labels.
        """
        import json
        from datetime import datetime, timezone

        import boto3

        s3 = boto3.client("s3")
        data = self._load_unlabeled_data(s3)

        logging.info("Loaded %s unlabeled samples from S3.", len(data))

        # If there are no unlabeled samples, we don't need to do anything else.
        if data.empty:
            return 0

        records = []
        for event_id, group in data.groupby("event_id"):
            predictions = []
            for _, row in group.iterrows():
                predictions.append(
                    self.get_fake_label(row["prediction"], ground_truth_quality),
                )

            record = {
                "groundTruthData": {
                    # For testing purposes, we will generate a random
                    # label for each request.
                    "data": predictions,
                    "encoding": "CSV",
                },
                "eventMetadata": {
                    # This value should match the id of the request
                    # captured by the endpoint.
                    "eventId": event_id,
                },
                "eventVersion": "0",
            }

            records.append(json.dumps(record))

        ground_truth_payload = "\n".join(records)
        upload_time = datetime.now(tz=timezone.utc)
        uri = (
            "/".join(self.ground_truth_uri.split("/")[3:])
            + f"{upload_time:%Y/%m/%d/%H/%M%S}.jsonl"
        )

        s3.put_object(
            Body=ground_truth_payload,
            Bucket=self.ground_truth_uri.split("/")[2],
            Key=uri,
        )

        return len(data)

    def save(self, model_input: pd.DataFrame, model_output: list) -> None:
        """Not implemented.

        Models hosted on Sagemaker automatically capture data, so we don't need to
        implement this method.
        """

    def invoke(self, payload: list | dict) -> dict | None:
        """Make a prediction request to the Sagemaker endpoint."""
        logging.info('Running prediction on "%s"...', self.target)

        response = self.deployment_client.predict(self.target, payload)
        df = pd.DataFrame(response["predictions"])[["prediction", "confidence"]]

        logging.info("\n%s", df)

        return df.to_json()

    def deploy(self, model_uri: str, model_version: str) -> None:
        """Deploy the model to Sagemaker.

        This function creates a new Sagemaker Model, Sagemaker Endpoint Configuration,
        and Sagemaker Endpoint to serve the latest version of the model.

        If the endpoint already exists, this function will update it with the latest
        version of the model.
        """
        from mlflow.exceptions import MlflowException

        deployment_configuration = {
            "instance_type": "ml.m4.xlarge",
            "instance_count": 1,
            "synchronous": True,
            # We want to archive resources associated with the endpoint that become
            # inactive as the result of updating an existing deployment.
            "archive": True,
            # Notice how we are storing the version number as a tag.
            "tags": {"version": model_version},
        }

        # If the data capture destination is defined, we can configure the Sagemaker
        # endpoint to capture data.
        if self.data_capture_uri is not None:
            deployment_configuration["data_capture_config"] = {
                "EnableCapture": True,
                "InitialSamplingPercentage": 100,
                "DestinationS3Uri": self.data_capture_uri,
                "CaptureOptions": [
                    {"CaptureMode": "Input"},
                    {"CaptureMode": "Output"},
                ],
                "CaptureContentTypeHeader": {
                    "CsvContentTypes": ["text/csv", "application/octect-stream"],
                    "JsonContentTypes": [
                        "application/json",
                        "application/octect-stream",
                    ],
                },
            }

        if self.assume_role:
            deployment_configuration["execution_role_arn"] = self.assume_role

        try:
            # Let's return the deployment with the name of the endpoint we want to
            # create. If the endpoint doesn't exist, this function will raise an
            # exception.
            deployment = self.deployment_client.get_deployment(self.target)

            # We now need to check whether the model we want to deploy is already
            # associated with the endpoint.
            if self._is_sagemaker_model_running(deployment, model_version):
                logging.info(
                    'Enpoint "%s" is already running model "%s".',
                    self.target,
                    model_version,
                )
            else:
                # If the model we want to deploy is not associated with the endpoint,
                # we need to update the current deployment to replace the previous model
                # with the new one.
                self._update_sagemaker_deployment(
                    deployment_configuration,
                    model_uri,
                    model_version,
                )
        except MlflowException:
            # If the endpoint doesn't exist, we can create a new deployment.
            self._create_sagemaker_deployment(
                deployment_configuration,
                model_uri,
                model_version,
            )

    def _get_boto3_client(self, service):
        """Return a boto3 client for the specified service.

        If the `assume_role` parameter is provided, this function will assume the role
        and return a new client with temporary credentials.
        """
        import boto3

        if not self.assume_role:
            return boto3.client(service)

        # If we have to assume a role, we need to create a new
        # Security Token Service (STS)
        sts_client = boto3.client("sts")

        # Let's use the STS client to assume the role and return
        # temporary credentials
        credentials = sts_client.assume_role(
            RoleArn=self.assume_role,
            RoleSessionName="mlschool-session",
        )["Credentials"]

        # We can use the temporary credentials to create a new session
        # from where to create the client for the target service.
        session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

        return session.client(service)

    def _is_sagemaker_model_running(self, deployment, version):
        """Check if the model is already running in Sagemaker.

        This function will check if the current model is already associated with a
        running Sagemaker endpoint.
        """
        sagemaker_client = self._get_boto3_client(service="sagemaker")

        # Here, we're assuming there's only one production variant associated with
        # the endpoint. This code will need to be updated if an endpoint could have
        # multiple variants.
        variant = deployment.get("ProductionVariants", [])[0]

        # From the variant, we can get the ARN of the model associated with the
        # endpoint.
        model_arn = sagemaker_client.describe_model(
            ModelName=variant.get("VariantName"),
        ).get("ModelArn")

        # With the model ARN, we can get the tags associated with the model.
        tags = sagemaker_client.list_tags(ResourceArn=model_arn).get("Tags", [])

        # Finally, we can check whether the model has a `version` tag that matches
        # the model version we're trying to deploy.
        model = next(
            (
                tag["Value"]
                for tag in tags
                if (tag["Key"] == "version" and tag["Value"] == version)
            ),
            None,
        )

        return model is not None

    def _create_sagemaker_deployment(
        self,
        deployment_configuration,
        model_uri,
        model_version,
    ):
        """Create a new Sagemaker deployment using the supplied configuration."""
        logging.info(
            'Creating endpoint "%s" with model "%s"...',
            self.target,
            model_version,
        )

        self.deployment_client.create_deployment(
            name=self.target,
            model_uri=model_uri,
            flavor="python_function",
            config=deployment_configuration,
        )

    def _update_sagemaker_deployment(
        self,
        deployment_configuration,
        model_uri,
        model_version,
    ):
        """Update an existing Sagemaker deployment using the supplied configuration."""
        logging.info(
            'Updating endpoint "%s" with model "%s"...',
            self.target,
            model_version,
        )

        # If you wanted to implement a staged rollout, you could extend the deployment
        # configuration with a `mode` parameter with the value
        # `mlflow.sagemaker.DEPLOYMENT_MODE_ADD` to create a new production variant. You
        # can then route some of the traffic to the new variant using the Sagemaker SDK.
        self.deployment_client.update_deployment(
            name=self.target,
            model_uri=model_uri,
            flavor="python_function",
            config=deployment_configuration,
        )

    def _load_unlabeled_data(self, s3):
        """Load any unlabeled data from the specified S3 location.

        This function will load the data captured from the endpoint during inference
        that does not have a corresponding ground truth information.
        """
        data = self._load_collected_data(s3)
        return data if data.empty else data[data["species"].isna()]

    def _load_collected_data(self, s3):
        """Load data from the endpoint and merge it with its ground truth."""
        data = self._load_collected_data_files(s3)
        ground_truth = self._load_ground_truth_files(s3)

        if len(data) == 0:
            return pd.DataFrame()

        if len(ground_truth) > 0:
            ground_truth = ground_truth.explode("species")
            data["index"] = data.groupby("event_id").cumcount()
            ground_truth["index"] = ground_truth.groupby("event_id").cumcount()

            data = data.merge(
                ground_truth,
                on=["event_id", "index"],
                how="left",
            )
            data = data.rename(columns={"species_y": "species"}).drop(
                columns=["species_x", "index"],
            )

        return data

    def _load_ground_truth_files(self, s3):
        """Load the ground truth data from the specified S3 location."""

        def process(row):
            data = row["groundTruthData"]["data"]
            event_id = row["eventMetadata"]["eventId"]

            return pd.DataFrame({"event_id": [event_id], "species": [data]})

        df = self._load_files(s3, self.ground_truth_uri)

        if df is None:
            return pd.DataFrame()

        processed_dfs = [process(row) for _, row in df.iterrows()]

        return pd.concat(processed_dfs, ignore_index=True)

    def _load_collected_data_files(self, s3):
        """Load the data captured from the endpoint during inference."""

        def process_row(row):
            date = row["eventMetadata"]["inferenceTime"]
            event_id = row["eventMetadata"]["eventId"]
            input_data = json.loads(row["captureData"]["endpointInput"]["data"])
            output_data = json.loads(row["captureData"]["endpointOutput"]["data"])

            if "instances" in input_data:
                df = pd.DataFrame(input_data["instances"])
            elif "inputs" in input_data:
                df = pd.DataFrame(input_data["inputs"])
            else:
                df = pd.DataFrame(
                    input_data["dataframe_split"]["data"],
                    columns=input_data["dataframe_split"]["columns"],
                )

            df = pd.concat(
                [
                    df,
                    pd.DataFrame(output_data["predictions"]),
                ],
                axis=1,
            )

            df["date"] = date
            df["event_id"] = event_id
            df["species"] = None
            return df

        df = self._load_files(s3, self.data_capture_uri)

        if df is None:
            return pd.DataFrame()

        processed_dfs = [process_row(row) for _, row in df.iterrows()]

        # Concatenate all processed DataFrames
        result_df = pd.concat(processed_dfs, ignore_index=True)
        return result_df.sort_values(by="date", ascending=False).reset_index(drop=True)

    def _load_files(self, s3, s3_uri):
        """Load every file stored in the supplied S3 location.

        This function will recursively return the contents of every file stored under
        the specified location. The function assumes that the files are stored in JSON
        Lines format.
        """
        bucket = s3_uri.split("/")[2]
        prefix = "/".join(s3_uri.split("/")[3:])

        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

        files = [
            obj["Key"]
            for page in pages
            if "Contents" in page
            for obj in page["Contents"]
        ]

        if len(files) == 0:
            return None

        dfs = []
        for file in files:
            obj = s3.get_object(Bucket=bucket, Key=file)
            data = obj["Body"].read().decode("utf-8")

            json_lines = data.splitlines()

            # Parse each line as a JSON object and collect into a list
            dfs.append(pd.DataFrame([json.loads(line) for line in json_lines]))

        # Concatenate all DataFrames into a single DataFrame
        return pd.concat(dfs, ignore_index=True)


class Mock(Backend):
    """Mock implementation of the Backend abstract class.

    This class is helpful for testing purposes to simulate access to
    a production backend.
    """

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        """Initialize the mock backend."""

    def load(self, limit: int) -> pd.DataFrame | None:  # noqa: ARG002
        """Return fake data for testing purposes."""
        return pd.DataFrame(
            [
                {
                    "island": "Torgersen",
                    "culmen_length_mm": 38.6,
                    "culmen_depth_mm": 21.2,
                    "flipper_length_mm": 191,
                    "body_mass_g": 3800,
                    "sex": "MALE",
                    "ground_truth": "Adelie",
                    "prediction": "Adelie",
                },
                {
                    "island": "Torgersen",
                    "culmen_length_mm": 34.6,
                    "culmen_depth_mm": 21.1,
                    "flipper_length_mm": 198,
                    "body_mass_g": 4400,
                    "sex": "MALE",
                    "ground_truth": "Adelie",
                    "prediction": "Adelie",
                },
                {
                    "island": "Torgersen",
                    "culmen_length_mm": 36.6,
                    "culmen_depth_mm": 17.8,
                    "flipper_length_mm": 185,
                    "body_mass_g": 3700,
                    "sex": "FEMALE",
                    "ground_truth": "Adelie",
                    "prediction": "Adelie",
                },
                {
                    "island": "Torgersen",
                    "culmen_length_mm": 38.7,
                    "culmen_depth_mm": 19,
                    "flipper_length_mm": 195,
                    "body_mass_g": 3450,
                    "sex": "FEMALE",
                    "ground_truth": "Adelie",
                    "prediction": "Adelie",
                },
                {
                    "island": "Torgersen",
                    "culmen_length_mm": 42.5,
                    "culmen_depth_mm": 20.7,
                    "flipper_length_mm": 197,
                    "body_mass_g": 4500,
                    "sex": "MALE",
                    "ground_truth": "Adelie",
                    "prediction": "Adelie",
                },
            ],
        )

    def save(self, model_input: pd.DataFrame, model_output: list) -> None:
        """Not implemented."""

    def label(self, ground_truth_quality: float = 0.8) -> int:
        """Not implemented."""

    def invoke(self, payload: list | dict) -> dict | None:
        """Not implemented."""

    def deploy(self, model_uri: str, model_version: str) -> None:
        """Not implemented."""
