from bootstrapvz.common import task_groups
import tasks.packages
import tasks.connection
import tasks.host
import tasks.ami
import tasks.ebs
import tasks.filesystem
import tasks.boot
import tasks.network
import tasks.initd
from bootstrapvz.common.tasks import volume
from bootstrapvz.common.tasks import filesystem
from bootstrapvz.common.tasks import boot
from bootstrapvz.common.tasks import grub
from bootstrapvz.common.tasks import initd
from bootstrapvz.common.tasks import loopback
from bootstrapvz.common.tasks import kernel


def validate_manifest(data, validator, error):
	import os.path
	validator(data, os.path.join(os.path.dirname(__file__), 'manifest-schema.yml'))

	from bootstrapvz.common.bytes import Bytes
	if data['volume']['backing'] == 'ebs':
		volume_size = Bytes(0)
		for key, partition in data['volume']['partitions'].iteritems():
			if key != 'type':
				volume_size += Bytes(partition['size'])
		if int(volume_size % Bytes('1GiB')) != 0:
			msg = ('The volume size must be a multiple of 1GiB when using EBS backing')
			error(msg, ['volume', 'partitions'])
	else:
		validator(data, os.path.join(os.path.dirname(__file__), 'manifest-schema-s3.yml'))

	bootloader = data['system']['bootloader']
	virtualization = data['provider']['virtualization']
	backing = data['volume']['backing']
	partition_type = data['volume']['partitions']['type']
	enhanced_networking = data['provider']['enhanced_networking'] if 'enhanced_networking' in data['provider'] else None

	if virtualization == 'pvm' and bootloader != 'pvgrub':
		error('Paravirtualized AMIs only support pvgrub as a bootloader', ['system', 'bootloader'])

	if backing != 'ebs' and virtualization == 'hvm':
			error('HVM AMIs currently only work when they are EBS backed', ['volume', 'backing'])

	if backing == 's3' and partition_type != 'none':
			error('S3 backed AMIs currently only work with unpartitioned volumes', ['system', 'bootloader'])

	if enhanced_networking == 'simple' and virtualization != 'hvm':
			error('Enhanced networking only works with HVM virtualization', ['provider', 'virtualization'])


def resolve_tasks(taskset, manifest):
	taskset.update(task_groups.get_standard_groups(manifest))
	taskset.update(task_groups.ssh_group)

	taskset.update([tasks.host.AddExternalCommands,
	                tasks.packages.DefaultPackages,
	                tasks.connection.SilenceBotoDebug,
	                tasks.connection.GetCredentials,
	                tasks.ami.AMIName,
	                tasks.connection.Connect,

	                boot.BlackListModules,
	                boot.DisableGetTTYs,
	                initd.AddExpandRoot,
	                initd.RemoveHWClock,
	                initd.InstallInitScripts,

	                tasks.ami.RegisterAMI,
	                ])

	from bootstrapvz.common.releases import wheezy
	if manifest.release <= wheezy:
		# The default DHCP client `isc-dhcp' doesn't work properly on wheezy and earlier
		taskset.add(tasks.network.InstallDHCPCD)
		taskset.add(tasks.network.EnableDHCPCDDNS)

	if manifest.provider.get('install_init_scripts', True):
		taskset.add(tasks.initd.AddEC2InitScripts)

	if manifest.volume['partitions']['type'] != 'none':
		taskset.add(initd.AdjustExpandRootScript)

	if manifest.system['bootloader'] == 'pvgrub':
		taskset.add(grub.AddGrubPackage)
		taskset.add(tasks.boot.ConfigurePVGrub)

	if manifest.volume['backing'].lower() == 'ebs':
		taskset.update([tasks.host.GetInstanceMetadata,
		                tasks.ebs.Create,
		                tasks.ebs.Snapshot,
		                ])
		taskset.add(tasks.ebs.Attach)
		taskset.discard(volume.Attach)

	if manifest.volume['backing'].lower() == 's3':
		taskset.update([loopback.AddRequiredCommands,
		                tasks.host.SetRegion,
		                loopback.Create,
		                tasks.filesystem.S3FStab,
		                tasks.ami.BundleImage,
		                tasks.ami.UploadImage,
		                tasks.ami.RemoveBundle,
		                ])
		taskset.discard(filesystem.FStab)

	if manifest.provider.get('enhanced_networking', None) == 'simple':
		taskset.update([kernel.AddDKMSPackages,
		                tasks.network.InstallEnhancedNetworking,
		                kernel.UpdateInitramfs])

	taskset.update([filesystem.Format,
	                volume.Delete,
	                ])


def resolve_rollback_tasks(taskset, manifest, completed, counter_task):
	taskset.update(task_groups.get_standard_rollback_tasks(completed))
	counter_task(taskset, tasks.ebs.Create, volume.Delete)
	counter_task(taskset, tasks.ebs.Attach, volume.Detach)
	counter_task(taskset, tasks.ami.BundleImage, tasks.ami.RemoveBundle)
