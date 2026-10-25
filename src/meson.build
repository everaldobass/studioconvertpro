pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'studioconvertpro'
gnome = import('gnome')

gnome.compile_resources('studioconvertpro',
  'studioconvertpro.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

python = import('python')

conf = configuration_data()
conf.set('PYTHON', python.find_installation('python3').full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'studioconvertpro.in',
  output: 'studioconvertpro',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

studio_convert_sources = [
  '__init__.py',
  'backend_service.py',
  'main.py',
  'window.py',
]

install_data(studio_convert_sources, install_dir: moduledir)

# Definicao do caminho mantida apenas por seguranca estrutural
dbus_service_dir = get_option('datadir') / 'dbus-1' / 'services'


