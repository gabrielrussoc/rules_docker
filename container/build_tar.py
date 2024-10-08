# Copyright 2015 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This tool build tar files from a list of inputs."""

from contextlib import contextmanager
import argparse
import os
import os.path
import sys
import re
import tempfile

from container.vendor import archive
from container.vendor import tarfile

class TarFile(object):
  """A class to generates a Docker layer."""

  class DebError(Exception):
    pass

  PKG_NAME_RE = re.compile(r'Package:\s*(?P<pkg_name>\w+).*')
  DPKG_STATUS_DIR = '/var/lib/dpkg/status.d'
  PKG_METADATA_FILE = 'control'

  @staticmethod
  def parse_pkg_name(metadata, filename):
    pkg_name_match = TarFile.PKG_NAME_RE.match(metadata)
    if pkg_name_match:
      return pkg_name_match.group('pkg_name')
    else:
      return os.path.basename(os.path.splitext(filename)[0])

  def __init__(self, output, directory, compression):
    self.directory = directory
    self.output = output
    self.compression = compression

  def __enter__(self):
    self.tarfile = archive.TarFileWriter(self.output, self.compression)
    return self

  def __exit__(self, t, v, traceback):
    self.tarfile.close()

  def add_file(self, f, destfile, mode=None, ids=None, names=None):
    """Add a file to the tar file.

    Args:
       f: the file to add to the layer
       destfile: the name of the file in the layer
       mode: force to set the specified mode, by
          default the value from the source is taken.
       ids: (uid, gid) for the file to set ownership
       names: (username, groupname) for the file to set ownership.
    `f` will be copied to `self.directory/destfile` in the layer.
    """
    dest = destfile.lstrip('/')  # Remove leading slashes
    if self.directory and self.directory != '/':
      dest = self.directory.lstrip('/') + '/' + dest
    # If mode is unspecified, derive the mode from the file's mode.
    if mode is None:
      mode = 0o755 if os.access(f, os.X_OK) else 0o644
    if ids is None:
      ids = (0, 0)
    if names is None:
      names = ('', '')
    dest = os.path.normpath(dest)
    self.tarfile.add_file(
        dest,
        file_content=f,
        mode=mode,
        uid=ids[0],
        gid=ids[1],
        uname=names[0],
        gname=names[1])

  def add_empty_file(self, destfile, mode=None, ids=None, names=None):
    """Add a file to the tar file.

    Args:
       destfile: the name of the file in the layer
       mode: force to set the specified mode, by
          default the value from the source is taken.
       ids: (uid, gid) for the file to set ownership
       names: (username, groupname) for the file to set ownership.

    An empty file will be created as `destfile` in the layer.
    """
    dest = destfile.lstrip('/')  # Remove leading slashes
    # If mode is unspecified, assume read only
    if mode is None:
      mode = 0o644
    if ids is None:
      ids = (0, 0)
    if names is None:
      names = ('', '')
    dest = os.path.normpath(dest)
    self.tarfile.add_file(
        dest,
        content='',
        mode=mode,
        uid=ids[0],
        gid=ids[1],
        uname=names[0],
        gname=names[1])

  def add_tar(self, tar):
    """Merge a tar file into the destination tar file.

    All files presents in that tar will be added to the output file
    under self.directory/path. No user name nor group name will be
    added to the output.

    Args:
      tar: the tar file to add
    """
    root = None
    if self.directory and self.directory != '/':
      root = self.directory
    self.tarfile.add_tar(tar, numeric=True, root=root)

  def add_link(self, symlink, destination):
    """Add a symbolic link pointing to `destination`.

    Args:
      symlink: the name of the symbolic link to add.
      destination: where the symbolic link point to.
    """
    symlink = os.path.normpath(symlink)
    self.tarfile.add_file(symlink, tarfile.SYMTYPE, link=destination)

  @contextmanager
  def write_temp_file(self, data, suffix='tar', mode='wb'):
    (_, tmpfile) = tempfile.mkstemp(suffix=suffix)
    try:
      with open(tmpfile, mode='wb') as f:
        f.write(data)
      yield tmpfile
    finally:
      os.remove(tmpfile)

  def add_pkg_metadata(self, metadata_tar, deb):
    try:
      with tarfile.open(metadata_tar) as tar:
        # Metadata is expected to be in a file.
        control_file_member = filter(lambda f: os.path.basename(f.name) == TarFile.PKG_METADATA_FILE, tar.getmembers())
        if not control_file_member:
           raise self.DebError(deb + ' does not Metadata File!')
        control_file = tar.extractfile(control_file_member[0])
        metadata = ''.join(control_file.readlines())
        destination_file = os.path.join(TarFile.DPKG_STATUS_DIR,
                                        TarFile.parse_pkg_name(metadata, deb))
        with self.write_temp_file(data=metadata) as metadata_file:
          self.add_file(metadata_file, destination_file)
    except (KeyError, TypeError) as e:
      raise self.DebError(deb + ' contains invalid Metadata! Exeception {0}'.format(e))
    except Exception as e:
      raise self.DebError('Unknown Exception {0}. Please report an issue at'
                          ' github.com/bazelbuild/rules_docker.'.format(e))

  def add_deb(self, deb):
    """Extract a debian package in the output tar.

    All files presents in that debian package will be added to the
    output tar under the same paths. No user name nor group names will
    be added to the output.

    Args:
      deb: the tar file to add

    Raises:
      DebError: if the format of the deb archive is incorrect.
    """
    pkg_data_found = False
    pkg_metadata_found = False
    with archive.SimpleArFile(deb) as arfile:
      current = arfile.next()
      while current:
        parts = current.filename.split(".")
        name = parts[0]
        ext = '.'.join(parts[1:])
        if name == 'data':
          pkg_data_found = True
          # Add pkg_data to image tar
          with self.write_temp_file(suffix=ext, data=current.data) as tmpfile:
            self.add_tar(tmpfile)
        elif name == 'control':
          pkg_metadata_found = True
          # Add metadata file to image tar
          with self.write_temp_file(suffix=ext, data=current.data) as tmpfile:
            self.add_pkg_metadata(metadata_tar=tmpfile, deb=deb)
        current = arfile.next()

    if not pkg_data_found:
      raise self.DebError(deb + ' does not contains a data file!')
    if not pkg_metadata_found:
      raise self.DebError(deb + ' does not contains a control file!')


def main(FLAGS):
  # Parse modes arguments
  default_mode = None
  if FLAGS.mode:
    # Convert from octal
    default_mode = int(FLAGS.mode, 8)

  mode_map = {}
  if FLAGS.modes:
    for filemode in FLAGS.modes:
      (f, mode) = filemode.split('=', 1)
      if f[0] == '/':
        f = f[1:]
      mode_map[f] = int(mode, 8)

  default_ownername = ('', '')
  if FLAGS.owner_name:
    default_ownername = FLAGS.owner_name.split('.', 1)
  names_map = {}
  if FLAGS.owner_names:
    for file_owner in FLAGS.owner_names:
      (f, owner) = file_owner.split('=', 1)
      (user, group) = owner.split('.', 1)
      if f[0] == '/':
        f = f[1:]
      names_map[f] = (user, group)

  default_ids = FLAGS.owner.split('.', 1)
  default_ids = (int(default_ids[0]), int(default_ids[1]))
  ids_map = {}
  if FLAGS.owners:
    for file_owner in FLAGS.owners:
      (f, owner) = file_owner.split('=', 1)
      (user, group) = owner.split('.', 1)
      if f[0] == '/':
        f = f[1:]
      ids_map[f] = (int(user), int(group))

  # Add objects to the tar file
  with TarFile(FLAGS.output, FLAGS.directory, FLAGS.compression) as output:
    for f in FLAGS.file:
      (inf, tof) = f.split('=', 1)
      mode = default_mode
      ids = default_ids
      names = default_ownername
      map_filename = tof
      if tof[0] == '/':
        map_filename = tof[1:]
      if map_filename in mode_map:
        mode = mode_map[map_filename]
      if map_filename in ids_map:
        ids = ids_map[map_filename]
      if map_filename in names_map:
        names = names_map[map_filename]
      output.add_file(inf, tof, mode, ids, names)
    for f in FLAGS.empty_file:
      output.add_empty_file(f)
    for tar in FLAGS.tar:
      output.add_tar(tar)
    for deb in FLAGS.deb:
      output.add_deb(deb)
    for link in FLAGS.link:
      l = link.split(':', 1)
      output.add_link(l[0], l[1])


if __name__ == '__main__':
  parser = argparse.ArgumentParser(fromfile_prefix_chars="@")
  parser.add_argument('--output', type=str, required=True,
    help='The output file, mandatory')

  parser.add_argument('--file', default=[], type=str, action='append',
    help='A file to add to the layer')

  parser.add_argument('--empty_file', type=str, default=[], action='append',
    help='An empty file to add to the layer')

  parser.add_argument('--mode', type=str,
    help='Force the mode on the added files (in octal).')

  parser.add_argument('--tar', type=str, default=[], action='append',
    help='A tar file to add to the layer')

  parser.add_argument('--deb', type=str, default=[], action='append',
    help='A debian package to add to the layer')

  def validate_link(l):
    if l.find(':') > 0:
      return l
    raise argparse.ArgumentTypeError("--link value should contain a : separator")

  parser.add_argument('--link', type=validate_link, default=[], action='append',
    help='Add a symlink a inside the layer ponting to b if a:b is specified')

  parser.add_argument('--directory', type=str,
    help='Directory in which to store the file inside the layer')

  parser.add_argument('--compression', type=str,
    help='Compression (`gz` or `bz2`), default is none.')

  parser.add_argument('--modes', type=str, default=None, action='append',
    help='Specific mode to apply to specific file (from the file argument),'
    ' e.g., path/to/file=0o455.')

  parser.add_argument('--owners', type=str, default=None, action='append',
    help='Specific mode to apply to specific file (from the file argument),'
    ' e.g., path/to/file=0o455.')

  parser.add_argument('--owner', type=str, default='0.0',
    help='Specify the numeric default owner of all files, e.g., 0.0')

  parser.add_argument('--owner_name', type=str,
    help='Specify the owner name of all files, e.g. root.root.')

  parser.add_argument('--owner_names', type=str, default=None, action='append',
    help='Specify the owner names of individual files, e.g. path/to/file=root.root.')

  main(parser.parse_args())
