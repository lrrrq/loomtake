from dataclasses import dataclass


@dataclass(frozen=True)
class SourceShot:
    scene_type: str
    source_file: str
    source_sheet: str
    source_row: int
    shot_no: str
    structure: str
    form: str
    duration_seconds: str
    effect: str
    shot_size: str
    camera_movement: str
    description: str
    audio: str
    note: str
    reference_image_path: str = ""
    effect_reference_path: str = ""

    def prompt_dict(self) -> dict:
        return {
            "scene_type": self.scene_type,
            "source_file": self.source_file,
            "source_sheet": self.source_sheet,
            "source_row": self.source_row,
            "structure": self.structure,
            "form": self.form,
            "duration_seconds": self.duration_seconds,
            "effect": self.effect,
            "shot_size": self.shot_size,
            "camera_movement": self.camera_movement,
            "description": self.description,
            "audio": self.audio,
            "note": self.note,
            "reference_image_path": self.reference_image_path,
            "effect_reference_path": self.effect_reference_path,
        }

