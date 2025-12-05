import os
from sigma.processing.pipeline import ProcessingPipeline, ProcessingItem
from sigma.processing.transformations import FieldMappingTransformation

class SigmaPipelineConfig:
    def __init__(self, name: str, priority: int, allowed_backends: frozenset, field_mappings: dict) -> None:
        """
        Initializes the SigmaPipelineConfig with the pipeline name, priority, allowed backends, and field mappings.

        :param name: Name of the processing pipeline.
        :param priority: Priority of the pipeline.
        :param allowed_backends: Set of allowed backend identifiers.
        :param field_mappings: Dictionary mapping field names to their converted names.
        """
        self.name = name
        self.priority = priority
        self.allowed_backends = allowed_backends
        self.field_mappings = field_mappings

    def create_pipeline(self) -> ProcessingPipeline:
        """
        Creates and returns a ProcessingPipeline object configured with the given field mappings.

        :return: Configured ProcessingPipeline object.
        """
        if not self.field_mappings:
            raise ValueError("Field mappings must be provided and cannot be empty.")

        return ProcessingPipeline(
            name=self.name,
            allowed_backends=self.allowed_backends,
            priority=self.priority,
            items=[
                ProcessingItem(
                    identifier="field_mapping",
                    transformation=FieldMappingTransformation(self.field_mappings)
                )
            ]
        )

class SigmaPipeline(SigmaPipelineConfig):
    def __init__(self, field_mappings: dict) -> None:
        """
        Initializes the SigmaPipeline with provided field mappings.

        :param field_mappings: Dictionary mapping Sigma fields to schema fields.
        """
        super().__init__(name="clickhouse pipeline", priority=20, allowed_backends=frozenset(), field_mappings=field_mappings)

    def create_pipeline(self) -> ProcessingPipeline:
        """
        Overrides the create_pipeline method to provide additional customization if needed.
        """
        return super().create_pipeline()
