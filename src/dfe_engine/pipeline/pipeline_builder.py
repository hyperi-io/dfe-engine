"""Build every ingestion pipeline a DFE config lists."""

from pathlib import Path

from scalo.logger import logger

from .pipeline import Pipeline


class PipelineBuilder:
    """Builds every ingestion pipeline a DFE config lists."""

    def __init__(
        self,
        dfe_config: dict,
        ingestion_output_path: str | Path | None = None,
        ingestion_pipeline_template_path: str | Path | None = None,
        logger=None,
        # Optional extra configuration for ingestion pipelines
        extra_config: dict | None = None,
    ):
        if extra_config is None:
            extra_config = {}
        self.dfe_config = dfe_config
        self.ingestion_output_path = (
            ingestion_output_path
            if ingestion_output_path is not None
            else dfe_config.get("global_settings", {}).get("output")
        )
        self.ingestion_pipeline_template_path = ingestion_pipeline_template_path
        self.extra_config = extra_config

    def build(self):
        """Render and write each pipeline the config lists."""
        for pipeline_name, pipeline_config in self.dfe_config.get(
            "ingestion_pipelines", {}
        ).items():
            logger.info(f"Building pipeline: {pipeline_name}")
            pipeline = Pipeline(
                name=pipeline_name,
                dfe_config=self.dfe_config,
                pipeline_config=pipeline_config,
                output_dir=self.ingestion_output_path,
                logger=logger,
                ingestion_pipeline_template_path=self.ingestion_pipeline_template_path,
                extra_config=self.extra_config,
            )
            pipeline.build()
