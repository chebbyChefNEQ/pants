# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

# Enable `python -m pants ...` style execution ala `json.tool` or `venv`.
python_library(
  name='entry_point',
  sources=['__main__.py'],
  dependencies=[
    'src/python/pants/bin:pants_loader',
  ],
)

python_distribution(
  name='pants-packaged',
  dependencies=[
    ':dummy_c',
    ':entry_point',
    ':version',
    ':py_typed',
  ],
  # Because we have native code, this will cause the wheel to use whatever the ABI is for the
  # interpreter used to run setup.py, e.g. `cp36m-macosx_10_15_x86_64`.
  setup_py_commands=["bdist_wheel"],
  provides=setup_py(
    name='pantsbuild.pants',
    description='A scalable build tool for large, complex, heterogeneous repos.',
    # NB: by setting `ext_modules`, we signal to setup_py and bdist_wheel that this library
    # has native code. As a consequence, bdist_wheel pins the ABI (application binary interface)
    # used when creating the wheel, which is a good thing. We should be setting this ABI to ensure
    # consumers of pantsbuild.pants are using a compatible interpreter.
    # TODO(7344): the tuple syntax for ext_modules is deprecated. Use Extension once we support it.
    ext_modules=[('native_engine', {'sources': ['pants/dummy.c']})],
  ),
  entry_points={
    'console_scripts': {
      'pants': 'src/python/pants/bin:pants',
    },
  },
)

# NB: we use this to avoid clang/gcc complaining `error: no input files` when building
# `:pants-packaged`. We don't actually need to use any meaningful file here, though, because we
# use `entry_points` to link to the actual native code, so clang/gcc do not need to build any
# native code. This is just a dummy file.
resources(
  name="dummy_c",
  sources=['dummy.c'],
)

python_library(
  name='conftest',
  sources=['conftest.py'],
)

python_library(
  name='version',
  sources=['version.py'],
  dependencies = [
    ':version-resource',
  ],
)

resources(
  name='version-resource',
  sources=['VERSION'],
)

resources(
  name="py_typed",
  sources=["py.typed"],
)
