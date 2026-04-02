# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Redis Operator
# Build with: pyinstaller redis_operator.spec --clean --noconfirm

block_cipher = None

a = Analysis(
    ['launch.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('static',        'static'),
        ('tasks',         'tasks'),
        ('redis_bundled', 'redis_bundled'),
    ],
    hiddenimports=[
        # Flask / Werkzeug
        'flask',
        'flask.templating',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.routing',
        'werkzeug.exceptions',
        'jinja2',
        'markupsafe',
        'click',

        # APScheduler
        'apscheduler',
        'apscheduler.schedulers.background',
        'apscheduler.triggers.cron',
        'apscheduler.triggers.interval',
        'apscheduler.triggers.date',
        'apscheduler.executors.pool',
        'apscheduler.jobstores.memory',
        'apscheduler.jobstores.base',
        'apscheduler.events',
        'apscheduler.util',
        'six',

        # Redis
        'redis',
        'redis.client',
        'redis.connection',
        'redis.exceptions',

        # SQLAlchemy (APScheduler dependency)
        'sqlalchemy',
        'sqlalchemy.dialects.sqlite',

        # System tray
        'pystray',
        'pystray._win32',

        # Pillow
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',

        # Anthropic / HTTP
        'anthropic',
        'httpx',
        'httpcore',
        'certifi',
        'charset_normalizer',
        'anyio',

        # Timezone
        'tzlocal',

        # Tkinter (native file picker)
        'tkinter',
        'tkinter.filedialog',

        # Misc
        'pkg_resources',
        'dotenv',
        'concurrent.futures',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['test', 'unittest', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Redis Operator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='redis_operator.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Redis Operator',
)
