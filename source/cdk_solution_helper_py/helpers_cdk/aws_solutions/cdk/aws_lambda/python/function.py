# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
from pathlib import Path
from typing import List, Union

import aws_cdk.aws_iam as iam
from aws_cdk import (
    AssetHashType,
    BundlingOptions,
    DockerImage,
    Aws,
)
from aws_cdk.aws_lambda import Function, Runtime, RuntimeFamily, Code
from constructs import Construct

from aws_solutions.cdk.aws_lambda.python.bundling import SolutionsPythonBundling

DEFAULT_RUNTIME = Runtime.PYTHON_3_11
DEPENDENCY_EXCLUDES = ["*.pyc"]


class DirectoryHash:
    # fmt: off
    # NOSONAR - safe to hash; side-effect of collision is to create new bundle
    _hash = hashlib.sha1()  # nosec
    # fmt: on

    @classmethod
    def hash(cls, *directories: Path):
        # NOSONAR - safe to hash; see above
        DirectoryHash._hash = hashlib.sha1()  # nosec
        if isinstance(directories, Path):
            directories = [directories]
        for directory in sorted(directories):
            DirectoryHash._hash_dir(str(directory.absolute()))
        return DirectoryHash._hash.hexdigest()

    @classmethod
    def _hash_dir(cls, directory: Path):
        for path, dirs, files in os.walk(directory):
            for file in sorted(files):
                DirectoryHash._hash_file(Path(path) / file)
            for directory in sorted(dirs):
                DirectoryHash._hash_dir(str((Path(path) / directory).absolute()))
            break

    @classmethod
    def _hash_file(cls, file: Path):
        with file.open("rb") as f:
            while True:
                block = f.read(2 ** 10)
                if not block:
                    break
                DirectoryHash._hash.update(block)


class SolutionsPythonFunction(Function):
    """This is similar to aws-cdk/aws-lambda-python, however it handles local bundling"""

    def __init__(
        self,  # NOSONAR (python:S107) - allow large number of method parameters
        scope: Construct,
        construct_id: str,
        entrypoint: Path,
        function: str,
        libraries: Union[List[Path], Path, None] = None,
        **kwargs,
    ):
        self.scope = scope
        self.construct_id = construct_id
        self.source_path = entrypoint.parent

        # validate source path
        if not self.source_path.is_dir():
            raise ValueError(
                f"entrypoint {entrypoint} must not be a directory, but rather a .py file"
            )

        # validate libraries
        self.libraries = libraries or []
        self.libraries = (
            self.libraries if isinstance(self.libraries, list) else [self.libraries]
        )
        for lib in self.libraries:
            if lib.is_file():
                raise ValueError(
                    f"library {lib} must not be a file, but rather a directory"
                )

        # create default least privileged role for this function unless a role is passed
        if not kwargs.get("role"):
            kwargs["role"] = self._create_role()

        # python 3.7 is selected to support custom resources and inline code
        if not kwargs.get("runtime"):
            kwargs["runtime"] = DEFAULT_RUNTIME

        # validate that the user is using a python runtime for AWS Lambda
        if kwargs["runtime"].family != RuntimeFamily.PYTHON:
            raise ValueError(
                f"SolutionsPythonFunction must use a Python runtime ({kwargs['runtime']} was provided)"
            )

        # build the handler based on the entrypoint Path and function name
        if kwargs.get("handler"):
            raise ValueError(
                f"SolutionsPythonFunction expects a Path `entrypoint` (python file) and `function` (function in the entrypoint for AWS Lambda to invoke)"
            )
        else:
            kwargs["handler"] = f"{entrypoint.stem}.{function}"

        # build the code based on the entrypoint Path
        if kwargs.get("code"):
            raise ValueError(
                f"SolutionsPythonFunction expects a Path `entrypoint` (python file) and `function` (function in the entrypoint for AWS Lambda to invoke)"
            )

        bundling = SolutionsPythonBundling(
            self.source_path,
            self.libraries,
        )

        kwargs["code"] = self._get_code(bundling, runtime=kwargs["runtime"])

        # initialize the parent Function
        super().__init__(scope, construct_id, **kwargs)

    def _get_code(self, bundling: SolutionsPythonBundling, runtime: Runtime) -> Code:
        # try to create the code locally - if this fails, try using Docker
        code_parameters = {
            "path": str(self.source_path),
            "asset_hash_type": AssetHashType.CUSTOM,
            "asset_hash": DirectoryHash.hash(self.source_path, *self.libraries),
            "exclude": DEPENDENCY_EXCLUDES,
        }

        # to enable docker only bundling, use image=self._get_bundling_docker_image(bundling, runtime=runtime)
        code = Code.from_asset(
            bundling=BundlingOptions(
                image=DockerImage.from_registry(
                    "scratch"
                ),  # NOT USED - FOR NOW ALL BUNDLING IS LOCAL
                command=["NOT-USED"],
                entrypoint=["NOT-USED"],
                local=bundling,
            ),
            **code_parameters,
        )

        return code

    def _create_role(self) -> iam.Role:
        """
        Build a role that allows an AWS Lambda Function to log to CloudWatch
        :param name: The name of the role. The final name will be "{name}-Role"
        :return: aws_cdk.aws_iam.Role
        """
        role = iam.Role(
            self.scope,
            f"{self.construct_id}-Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={
                "LambdaFunctionServiceRolePolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            resources=[
                                f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:log-group:/aws/lambda/*"
                            ],
                        )
                    ]
                )
            },
        )
        role_l1_construct = role.node.find_child(id='Resource')
        role_l1_construct.add_metadata('guard', {'SuppressedRules': ['IAM_NO_INLINE_POLICY_CHECK']})
        return role
