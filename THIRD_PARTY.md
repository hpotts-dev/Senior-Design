# Third-party software

The Windows archive includes CPython, NumPy, OpenCV, Basler pypylon, Qt for Python Essentials and Shiboken. See the licenses and metadata in **.runtime/LICENSE.txt**, **.runtime/Lib/site-packages**, and the relevant **.dist-info** folders. Exact dependency versions are in **requirements-release.txt**.

Qt for Python and the Qt libraries are used under their open-source licenses, including LGPL v3 where applicable. The LGPL v3 and GPL v3 texts are in **licenses**. They are loaded dynamically from **.runtime/Lib/site-packages** and can be replaced with compatible builds. Users may modify the libraries and reverse engineer the application to debug those modifications as allowed by the LGPL. Upstream source is available from [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v6.10.2) and [Qt source releases](https://download.qt.io/official_releases/qt/6.10/6.10.2/single/).

