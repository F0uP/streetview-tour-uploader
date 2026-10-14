"""
Replaces PyInstaller's own hook for googleapiclient: the library ships the
discovery documents of all ~600 Google APIs (about 100 MB), the app only talks
to Street View Publish.
"""

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# googleapiclient.model reads the library's version from its metadata.
datas = copy_metadata('google_api_python_client')
datas += collect_data_files('googleapiclient.discovery_cache', includes=['documents/streetviewpublish.*.json'])
