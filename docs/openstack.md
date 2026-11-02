# Subir una imagen a OpenStack

Estado: la subida está implementada y probada contra una conexión falsa (propiedades, verificación
de checksum y formatos). **Nunca se ejecutó contra una nube real**, así que el criterio de F7 —subir
y arrancar la instancia— sigue abierto hasta que alguien lo haga con un proyecto a mano.

## Requisitos

```bash
pip install 'gira-iso[openstack]'      # o: uv sync --extra openstack
```

Sin el SDK, `gira-iso push` falla con código 3 (entorno) y dice cómo instalarlo. No es un error de
entrada.

## Credenciales

El SDK estándar de OpenStack. La forma recomendada es un `clouds.yaml`:

```yaml
# ~/.config/openstack/clouds.yaml
clouds:
  mycloud:
    auth:
      auth_url: https://keystone.example.com:5000/v3
      username: your-user
      password: your-password
      project_name: your-project
      user_domain_name: Default
      project_domain_name: Default
    region_name: RegionOne
    identity_api_version: 3
    interface: public
```

Con eso, `gira-iso push ... --cloud mycloud` no necesita nada más. También valen las variables
`OS_*` (`OS_AUTH_URL`, `OS_USERNAME`, `OS_PASSWORD`, `OS_PROJECT_NAME`…), que es lo que usa el CLI de
OpenStack.

`clouds.yaml`, `*.kubeconfig` y los archivos de credenciales están en `.gitignore`: nunca deben
versionarse. Verifica con `git check-ignore clouds.yaml` antes de crear uno dentro del repositorio.

## Subir

```bash
uv run gira-iso push builds/<timestamp>-<hash> --cloud mycloud
uv run gira-iso push builds/<timestamp>-<hash> --disk-format raw   # backend Ceph/RBD
uv run gira-iso push builds/<timestamp>-<hash> --name web-2026-11-01
```

Antes de enviar un byte, el checksum del artefacto se compara con el del manifest: una imagen
alterada o incompleta se rechaza. Las propiedades de Glance salen del manifest (`os_distro`,
`os_version`, `hw_qemu_guest_agent`, `hw_disk_bus`, más los hashes de spec, política y base), y el
nombre por defecto es `<os>-<version>-<12 primeros del spec_hash>`.

## Comprobar que arranca

La subida no prueba que la imagen arranque; eso es lo que falta para cerrar F7. Con el id que
imprime `push`:

```bash
openstack server create \
  --image <image-id> --flavor <sabor-con-disco> --key-name <tu-clave> \
  --nic net-id=<red> gira-iso-smoke
openstack server show gira-iso-smoke -f value -c status    # ACTIVE
ssh ubuntu@<ip> 'cloud-init status --wait && systemctl is-system-running'
```

## Test de integración (opt-in)

`tests/test_push_live.py` sube un build real y **no** borra la imagen (para poder arrancarla a
mano). Está marcado `network`, así que la suite normal lo deselecciona. Para ejecutarlo:

```bash
export OS_CLOUD=mycloud
export GIRA_ISO_INTEGRATION_BUILD=builds/<timestamp>-<hash>
uv run pytest -m network tests/test_push_live.py -s
```

Sin `GIRA_ISO_INTEGRATION_BUILD`, sin `openstacksdk` o sin credenciales, se salta: no falla la suite
en una máquina sin nube.
