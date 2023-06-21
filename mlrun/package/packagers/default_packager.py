# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import inspect
from types import MethodType
from typing import Any, List, Tuple, Type, Union

from mlrun.artifacts import Artifact
from mlrun.datastore import DataItem
from mlrun.utils import logger

from ..errors import MLRunPackagePackingError, MLRunPackageUnpackingError
from ..packager import Packager
from ..utils import DEFAULT_PICKLE_MODULE, ArtifactType, Pickler, TypeHintUtils


class DefaultPackager(Packager):
    """
    A default packager that handles all types and pack them as pickle files.

    The default packager implements all the required methods and have a default logic that should be satisfying most
    use cases. In order to work with this class, you shouldn't override the abstract class methods, but follow the
    guidelines below:

    * The class variable ``PACKABLE_OBJECT_TYPE``: The type of object this packager can pack and unpack (used in the
      ``is_packable`` method).
    * The class variable ``PACK_SUBCLASSES``: A flag that indicates whether to pack all subclasses of the
      ``PACKABLE_OBJECT_TYPE` (used in the ``is_packable`` method). Default is False.
    * The class variable ``DEFAULT_PACKING_ARTIFACT_TYPE``: The default artifact type to pack as. It is being returned
      from the method ``get_default_packing_artifact_type``
    * The class variable ``DEFAULT_UNPACKING_ARTIFACT_TYPE``: The default artifact type to unpack from. It is being
      returned from the method ``get_default_unpacking_artifact_type``.
    * The abstract class method ``pack``: The method is implemented to get the object and send it to the relevant
      packing method by the artifact type given using the following naming: "pack_<artifact_type>". (if artifact type
      was not provided, the default one will be used). For example: if the artifact type is "object" then the class
      method ``pack_object`` must be implemented. The signature of each pack class method must be::

          @classmethod
          def pack_x(cls, obj: Any, ...) -> Union[Tuple[Artifact, dict], dict]:
              pass

      Where 'x' is the artifact type, 'obj' is the object to pack, ... are additional custom log hint configurations and
      the returning values are the packed artifact and the instructions for unpacking it, or in case of result, the
      dictionary of the result with its key and value. The log hint configurations are sent by the user and shouldn't be
      mandatory, meaning they should have a default value (otherwise, the user will have to add them to every log hint).
    * The abstract class method ``unpack``: The method is implemented to get a ``DataItem`` and send it to the relevant
      unpacking method by the artifact type using the following naming: "unpack_<artifact_type>" (if artifact type was
      not provided, the default one will be used). For example: if the artifact type stored within the ``DataItem`` is
      "object" then the class method ``unpack_object`` must be implemented. The signature of each unpack class method
      must be::

          @classmethod
          def unpack_x(cls, data_item: mlrun.DataItem, ...) -> Any:
              pass

      Where 'x' is the artifact type, 'data_item' is the artifact's data item to unpack, ... are the instructions that
      were originally returned from ``pack_x`` (Each instruction must be optional (have a default value) to support
      objects from this type that were not packaged but customly logged) and the returning value is the unpacked
      object.
    * The abstract class method ``is_packable``: The method is implemented to validate the object type and artifact type
      automatically by the following rules:

      * Object type validation: Checking if the object type given match to the variable ``PACKABLE_OBJECT_TYPE`` with
        respect to the ``PACK_SUBCLASSES`` class variable.
      * Artifact type validation: Checking if the artifact type given is in the list returned from
        ``get_supported_artifact_types``.

    * The abstract class method ``is_unpackable``: The method is left as implemented in ``Packager``.
    * The abstract class method ``get_supported_artifact_types``: The method is implemented to look for all
      pack + unpack class methods implemented to collect the supported artifact types. If ``PackagerX`` has ``pack_y``,
      ``unpack_y`` and ``pack_z``, ``unpack_z`` that means the artifact types supported are 'y' and 'z'.
    * The abstract class method ``get_default_packing_artifact_type``: The method is implemented to return the new class
      variable ``DEFAULT_PACKING_ARTIFACT_TYPE``. You may still override the method if the default artifact type you
      need may change according to the object that's about to be packed.
    * The abstract class method ``get_default_unpacking_artifact_type``: The method is implemented to return the new
      class variable ``DEFAULT_UNPACKING_ARTIFACT_TYPE``. You may still override the method if the default artifact type
      you need may change according to the data item that's about to be unpacked.

    Important to remember (from the ``Packager`` docstring):

    * Linking artifacts ("extra data"): In order to link between packages (using the extra data or metrics spec
      attributes of an artifact), you should use the key as if it exists and as value ellipses (...). The manager will
      link all packages once it is done packing.

      For example, given extra data keys in the log hint as `extra_data`, setting them to an artifact should be::

          artifact = Artifact(key="my_artifact")
          artifact.spec.extra_data = {key: ... for key in extra_data}

    * Clearing outputs: Some packagers may produce files and temporary directories that should be deleted once done with
      logging the artifact. The packager can mark paths of files and directories to delete after logging using the class
      method ``future_clear``.

      For example, in the following packager's ``pack`` method we can write a text file, create an Artifact and then
      mark the text file to be deleted once the artifact is logged::

          with open("./some_file.txt", "w") as file:
              file.write("Pack me")
          artifact = Artifact(key="my_artifact")
          cls.future_clear(path="./some_file.txt")
          return artifact, None
    """

    # The type of object this packager can pack and unpack:
    PACKABLE_OBJECT_TYPE: Type = ...
    # A flag for indicating whether to pack all subclasses of the `PACKABLE_OBJECT_TYPE` as well:
    PACK_SUBCLASSES = False
    # The default artifact type to pack as:
    DEFAULT_PACKING_ARTIFACT_TYPE = ArtifactType.OBJECT
    # The default artifact type to unpack from:
    DEFAULT_UNPACKING_ARTIFACT_TYPE = ArtifactType.OBJECT

    @classmethod
    def get_default_packing_artifact_type(cls, obj: Any) -> str:
        """
        Get the default artifact type for packing an object of this packager.

        :param obj: The about to be packed object.

        :return: The default artifact type.
        """
        return cls.DEFAULT_PACKING_ARTIFACT_TYPE

    @classmethod
    def get_default_unpacking_artifact_type(cls, data_item: DataItem) -> str:
        """
        Get the default artifact type used for unpacking a data item holding an object of this packager. The method will
        be used when a data item is sent for unpacking without it being a package, but a simple url or an old / manually
        logged artifact.

        :param data_item: The about to be unpacked data item.

        :return: The default artifact type.
        """
        return cls.DEFAULT_UNPACKING_ARTIFACT_TYPE

    @classmethod
    def get_supported_artifact_types(cls) -> List[str]:
        """
        Get all the supported artifact types on this packager.

        :return: A list of all the supported artifact types.
        """
        # We look for pack + unpack method couples so there won't be a scenario where an object can be packed but not
        # unpacked. Result has no unpacking so we add it separately.
        return [
            key[len("pack_") :]
            for key in dir(cls)
            if key.startswith("pack_") and f"unpack_{key[len('pack_'):]}" in dir(cls)
        ] + ["result"]

    @classmethod
    def pack(
        cls,
        obj: Any,
        artifact_type: str = None,
        configurations: dict = None,
    ) -> Union[Tuple[Artifact, dict], dict]:
        """
        Pack an object as the given artifact type using the provided configurations.

        :param obj:            The object to pack.
        :param artifact_type:  Artifact type to log to MLRun. If passing `None`, the default artifact type will be used.
        :param configurations: Log hints configurations to pass to the packing method.

        :return: If the packed object is an artifact, a tuple of the packed artifact and unpacking instructions
                 dictionary. If the packed object is a result, a dictionary containing the result key and value.
        """
        # Get default artifact type in case it was not provided:
        if artifact_type is None:
            artifact_type = cls.get_default_packing_artifact_type(obj=obj)

        # Set empty dictionary in case no configurations were given:
        configurations = configurations or {}

        # Get the packing method according to the artifact type:
        pack_method = getattr(cls, f"pack_{artifact_type}")

        # Validate correct configurations were passed:
        cls._validate_method_arguments(
            method=pack_method,
            arguments=configurations,
            is_packing=True,
        )

        # Call the packing method and return the package:
        return pack_method(obj, **configurations)

    @classmethod
    def unpack(
        cls,
        data_item: DataItem,
        artifact_type: str = None,
        instructions: dict = None,
    ) -> Any:
        """
        Unpack the data item's artifact by the provided type using the given instructions.

        :param data_item:     The data input to unpack.
        :param artifact_type: The artifact type to unpack the data item as. If passing `None`, the default artifact type
                              will be used.
        :param instructions:  Additional instructions noted in the package to pass to the unpacking method.

        :return: The unpacked data item's object.

        :raise MLRunPackageUnpackingError: In case the packager could not unpack the data item.
        """
        # Get default artifact type in case it was not provided:
        if artifact_type is None:
            artifact_type = cls.get_default_unpacking_artifact_type(data_item=data_item)

        # Set empty dictionary in case no instructions were given:
        instructions = instructions or {}

        # Get the unpacking method according to the artifact type:
        unpack_method = getattr(cls, f"unpack_{artifact_type}")

        # Validate correct instructions were passed:
        cls._validate_method_arguments(
            method=unpack_method,
            arguments=instructions,
            is_packing=False,
        )

        # Call the unpacking method and return the object:
        return unpack_method(data_item, **instructions)

    @classmethod
    def is_packable(cls, obj: Any, artifact_type: str = None) -> bool:
        """
        Check if this packager can pack an object of the provided type as the provided artifact type.

        The method is implemented to validate the object's type and artifact type by checking if the object type given
        match to the variable ``PACKABLE_OBJECT_TYPE`` with respect to the ``PACK_SUBCLASSES`` class variable. If it
        does, it will check if the artifact type given is in the list returned from ``get_supported_artifact_types``.

        :param obj:           The object to pack.
        :param artifact_type: The artifact type to log the object as.

        :return: True if packable and False otherwise.
        """
        # Get the object's type:
        object_type = type(obj)

        # Check type (ellipses means any type):
        if cls.PACKABLE_OBJECT_TYPE is not ...:
            if not TypeHintUtils.is_matching(
                object_type=object_type,
                type_hint=cls.PACKABLE_OBJECT_TYPE,
                include_subclasses=cls.PACK_SUBCLASSES,
                reduce_type_hint=False,
            ):
                return False

        # Check the artifact type:
        if (
            artifact_type is not None
            and artifact_type not in cls.get_supported_artifact_types()
        ):
            return False

        # Packable:
        return True

    @classmethod
    def pack_object(
        cls,
        obj: Any,
        key: str,
        pickle_module_name: str = DEFAULT_PICKLE_MODULE,
    ) -> Tuple[Artifact, dict]:
        """
        Pack a python object, pickling it into a pkl file and store it in an artifact.

        :param obj:                The object to pack and log.
        :param key:                The artifact's key.
        :param pickle_module_name: The pickle module name to use for serializing the object.

        :return: The artifacts and it's pickling instructions.
        """
        # Pickle the object to file:
        pickle_path, instructions = Pickler.pickle(
            obj=obj, pickle_module_name=pickle_module_name
        )

        # Initialize an artifact to the pkl file:
        artifact = Artifact(key=key, src_path=pickle_path)

        # Add the pickle path to the clearing list:
        cls.add_future_clearing_path(path=pickle_path)

        return artifact, instructions

    @classmethod
    def pack_result(cls, obj: Any, key: str) -> dict:
        """
        Pack an object as a result.

        :param obj: The object to pack and log.
        :param key: The result's key.

        :return: The result dictionary.
        """
        return {key: obj}

    @classmethod
    def unpack_object(
        cls,
        data_item: DataItem,
        pickle_module_name: str = DEFAULT_PICKLE_MODULE,
        object_module_name: str = None,
        python_version: str = None,
        pickle_module_version: str = None,
        object_module_version: str = None,
    ) -> Any:
        """
        Unpack the data item's object, unpickle it using the instructions and return.

        Warnings of mismatching python and module versions between the original pickling interpreter and this one may be
        raised.

        :param data_item:             The data item holding the pkl file.
        :param pickle_module_name:    Module to use for unpickling the object.
        :param object_module_name:    The original object's module. Used to verify the current interpreter object module
                                      version match the pickled object version before unpickling the object.
        :param python_version:        The python version in which the original object was pickled. Used to verify the
                                      current interpreter python version match the pickled object version before
                                      unpickling the object.
        :param pickle_module_version: The pickle module version. Used to verify the current interpreter module version
                                      match the one who pickled the object before unpickling it.
        :param object_module_version: The original object's module version to match to the interpreter's module version.

        :return: The un-pickled python object.
        """
        # Get the pkl file to local directory:
        pickle_path = data_item.local()

        # Add the pickle path to the clearing list:
        cls.add_future_clearing_path(path=pickle_path)

        # Unpickle and return:
        return Pickler.unpickle(
            pickle_path=pickle_path,
            pickle_module_name=pickle_module_name,
            object_module_name=object_module_name,
            python_version=python_version,
            pickle_module_version=pickle_module_version,
            object_module_version=object_module_version,
        )

    @classmethod
    def _validate_method_arguments(
        cls, method: MethodType, arguments: dict, is_packing: bool
    ):
        """
        Validate keyword arguments to pass to a method. Used for validating log hint configurations for packing methods
        and instructions for unpacking methods.

        :param method:     The method to validate the arguments for.
        :param arguments:  Keyword arguments to validate.
        :param is_packing: Flag to know if the arguments came from packing or unpacking, to raise the correct exception
                           if validation failed.

        :raise MLRunPackagePackingError:   If there are missing configurations in the log hint.
        :raise MLRunPackageUnpackingError: If there are missing instructions in the artifact's spec.
        """
        # Get the possible and mandatory (arguments that has no default value) arguments from the functions:
        possible_arguments = inspect.signature(method).parameters
        mandatory_arguments = [
            name
            for name, parameter in possible_arguments.items()
            # If default value is `empty` it is mandatory:
            if parameter.default is inspect.Parameter.empty
            # Ignore the *args and **kwargs parameters:
            and parameter.kind
            not in [inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL]
        ]
        mandatory_arguments.remove("obj" if is_packing else "data_item")

        # Validate there are no missing arguments (only mandatory ones):
        missing_arguments = [
            mandatory_argument
            for mandatory_argument in mandatory_arguments
            if mandatory_argument not in arguments
        ]
        if missing_arguments:
            if is_packing:
                raise MLRunPackagePackingError(
                    f"The packager '{cls.__name__}' could not pack the package due to missing configurations: "
                    f"{', '.join(missing_arguments)}. Add the missing arguments to the log hint of this object in "
                    f"order to pack it. Make sure you pass a dictionary log hint and not a string in order to pass "
                    f"configurations in the log hint."
                )
            raise MLRunPackageUnpackingError(
                f"The packager '{cls.__name__}' could not unpack the package due to missing instructions: "
                f"{', '.join(missing_arguments)}. Missing instructions are likely due to an update in the packager's "
                f"code that not support the old implementation. This backward compatibility should not occur. To "
                f"overcome it, try to edit the instructions in the artifact's spec to enable unpacking it again."
            )

        # Validate all given arguments are correct:
        incorrect_arguments = [
            argument for argument in arguments if argument not in possible_arguments
        ]
        if incorrect_arguments:
            arguments_type = "configurations" if is_packing else "instructions"
            logger.warn(
                f"Unexpected {arguments_type} given for {cls.__name__}: {', '.join(incorrect_arguments)}. "
                f"Possible {arguments_type} are: {', '.join(possible_arguments.keys())}. The packager will try to "
                f"continue by ignoring the incorrect arguments."
            )
