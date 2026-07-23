import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Version:
    name: str
    value: str

    def components(self, as_int: bool = False) -> list:
        return [int(val) if as_int else val for val in self.value.split(".")]

    @property
    def major(self) -> int:
        component, *_ = self.components(as_int=True)
        return component

    @property
    def minor(self) -> int:
        _, component, *_ = self.components(as_int=True)
        return component

    @property
    def patch(self) -> int:
        *_, component = self.components(as_int=True)
        return component

    @classmethod
    def from_path(cls, dirpath: str, name: str):
        for file in os.listdir(dirpath):
            if file.lower().endswith("version"):
                filepath = os.path.join(dirpath, file)
                break
        else:
            raise ValueError("Version file not found for package name: " + name)

        with open(filepath, "r") as version_file:
            version_value = version_file.readline().strip()
            return cls(name=name, value=version_value)


try:
    version = Version.from_path(name="alissa-tools-github-revloop", dirpath=os.path.dirname(__file__))
except Exception:
    print("Version file not found for package name: alissa-tools-github-revloop, using 0.0.0")
    version = Version(name="alissa-tools-github-revloop", value="0.0.0")
