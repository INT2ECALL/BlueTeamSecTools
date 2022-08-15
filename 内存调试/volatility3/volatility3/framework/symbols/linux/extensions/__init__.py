# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import collections.abc
import logging
from typing import Generator, Iterable, Iterator, Optional, Tuple

from volatility3.framework import constants
from volatility3.framework import exceptions, objects, interfaces, symbols
from volatility3.framework.layers import linear
from volatility3.framework.objects import utility
from volatility3.framework.symbols import generic, linux
from volatility3.framework.symbols import intermed
from volatility3.framework.symbols.linux.extensions import elf

vollog = logging.getLogger(__name__)

# Keep these in a basic module, to prevent import cycles when symbol providers require them


class module(generic.GenericIntelProcess):

    def get_module_base(self):
        if self.has_member("core_layout"):
            return self.core_layout.base
        else:
            return self.module_core

    def get_init_size(self):
        if self.has_member("init_layout"):
            return self.init_layout.size

        elif self.has_member("init_size"):
            return self.init_size

        raise AttributeError("module -> get_init_size: Unable to determine .init section size of module")

    def get_core_size(self):
        if self.has_member("core_layout"):
            return self.core_layout.size

        elif self.has_member("core_size"):
            return self.core_size

        raise AttributeError("module -> get_core_size: Unable to determine core size of module")

    def get_module_core(self):
        if self.has_member("core_layout"):
            return self.core_layout.base
        elif self.has_member("module_core"):
            return self.module_core

        raise AttributeError("module -> get_module_core: Unable to get module core")

    def get_module_init(self):
        if self.has_member("init_layout"):
            return self.init_layout.base
        elif self.has_member("module_init"):
            return self.module_init

        raise AttributeError("module -> get_module_core: Unable to get module init")

    def get_name(self):
        """ Get the name of the module as a string """
        return utility.array_to_string(self.name)

    def _get_sect_count(self, grp):
        """ Try to determine the number of valid sections """
        arr = self._context.object(
            self.get_symbol_table().name + constants.BANG + "array",
            layer_name = self.vol.layer_name,
            offset = grp.attrs,
            subtype = self._context.symbol_space.get_type(self.get_symbol_table().name + constants.BANG + "pointer"),
            count = 25)

        idx = 0
        while arr[idx]:
            idx = idx + 1

        return idx

    def get_sections(self):
        """ Get sections of the module """
        if self.sect_attrs.has_member("nsections"):
            num_sects = self.sect_attrs.nsections
        else:
            num_sects = self._get_sect_count(self.sect_attrs.grp)

        arr = self._context.object(self.get_symbol_table().name + constants.BANG + "array",
                                   layer_name = self.vol.layer_name,
                                   offset = self.sect_attrs.attrs.vol.offset,
                                   subtype = self._context.symbol_space.get_type(self.get_symbol_table().name +
                                                                                 constants.BANG + 'module_sect_attr'),
                                   count = num_sects)

        for attr in arr:
            yield attr

    def get_symbols(self):
        if symbols.symbol_table_is_64bit(self._context, self.get_symbol_table().name):
            prefix = "Elf64_"
        else:
            prefix = "Elf32_"

        elf_table_name = intermed.IntermediateSymbolTable.create(self.context,
                                                                 self.config_path,
                                                                 "linux",
                                                                 "elf",
                                                                 native_types = None,
                                                                 class_types = elf.class_types)

        syms = self._context.object(
            self.get_symbol_table().name + constants.BANG + "array",
            layer_name = self.vol.layer_name,
            offset = self.section_symtab,
            subtype = self._context.symbol_space.get_type(elf_table_name + constants.BANG + prefix + "Sym"),
            count = self.num_symtab + 1)
        if self.section_strtab:
            for sym in syms:
                sym.set_cached_strtab(self.section_strtab)
                yield sym

    def get_symbol(self, wanted_sym_name):
        """ Get value for a given symbol name """
        for sym in self.get_symbols():
            sym_name = sym.get_name()
            sym_addr = sym.st_value
            if wanted_sym_name == sym_name:
                return sym_addr

    @property
    def section_symtab(self):
        if self.has_member("kallsyms"):
            return self.kallsyms.symtab
        elif self.has_member("symtab"):
            return self.symtab

        raise AttributeError("module -> symtab: Unable to get symtab")

    @property
    def num_symtab(self):
        if self.has_member("kallsyms"):
            return int(self.kallsyms.num_symtab)
        elif self.has_member("num_symtab"):
            return int(self.num_symtab)

        raise AttributeError("module -> num_symtab: Unable to determine number of symbols")

    @property
    def section_strtab(self):
        # Newer kernels
        if self.has_member("kallsyms"):
            return self.kallsyms.strtab
        # Older kernels
        elif self.has_member("strtab"):
            return self.strtab

        raise AttributeError("module -> strtab: Unable to get strtab")


class task_struct(generic.GenericIntelProcess):

    def add_process_layer(self, config_prefix: str = None, preferred_name: str = None) -> Optional[str]:
        """Constructs a new layer based on the process's DTB.

        Returns the name of the Layer or None.
        """

        parent_layer = self._context.layers[self.vol.layer_name]
        try:
            pgd = self.mm.pgd
        except exceptions.InvalidAddressException:
            return None

        if not isinstance(parent_layer, linear.LinearlyMappedLayer):
            raise TypeError("Parent layer is not a translation layer, unable to construct process layer")

        dtb, layer_name = parent_layer.translate(pgd)
        if not dtb:
            return None

        if preferred_name is None:
            preferred_name = self.vol.layer_name + f"_Process{self.pid}"

        # Add the constructed layer and return the name
        return self._add_process_layer(self._context, dtb, config_prefix, preferred_name)

    def get_process_memory_sections(self, heap_only: bool = False) -> Generator[Tuple[int, int], None, None]:
        """Returns a list of sections based on the memory manager's view of
        this task's virtual memory."""
        for vma in self.mm.get_mmap_iter():
            start = int(vma.vm_start)
            end = int(vma.vm_end)

            if heap_only and not (start <= self.mm.brk and end >= self.mm.start_brk):
                continue
            else:
                # FIXME: Check if this actually needs to be printed out or not
                vollog.info(f"adding vma: {start:x} {self.mm.brk:x} | {end:x} {self.mm.start_brk:x}")

            yield (start, end - start)

    @property
    def is_kernel_thread(self) -> bool:
        """Checks if this task is a kernel thread.

        Returns:
            bool: True, if this task is a kernel thread. Otherwise, False.
        """
        return (self.flags & constants.linux.PF_KTHREAD) != 0

    @property
    def is_thread_group_leader(self) -> bool:
        """Checks if this task is a thread group leader.

        Returns:
            bool: True, if this task is a thread group leader. Otherwise, False.
        """
        return self.tgid == self.pid

    @property
    def is_user_thread(self) -> bool:
        """Checks if this task is a user thread.

        Returns:
            bool: True, if this task is a user thread. Otherwise, False.
        """
        return not self.is_kernel_thread and self.tgid != self.pid

    def get_threads(self) -> Iterable[interfaces.objects.ObjectInterface]:
        """Returns a list of the task_struct based on the list_head
        thread_node structure."""

        task_symbol_table_name = self.get_symbol_table_name()

        # iterating through the thread_list from thread_group
        # this allows iterating through pointers to grab the
        # threads and using the thread_group offset to get the
        # corresponding task_struct
        for task in self.thread_group.to_list(
            f"{task_symbol_table_name}{constants.BANG}task_struct",
            "thread_group"
        ):
            yield task

class fs_struct(objects.StructType):

    def get_root_dentry(self):
        # < 2.6.26
        if self.has_member("rootmnt"):
            return self.root
        elif self.root.has_member("dentry"):
            return self.root.dentry

        raise AttributeError("Unable to find the root dentry")

    def get_root_mnt(self):
        # < 2.6.26
        if self.has_member("rootmnt"):
            return self.rootmnt
        elif self.root.has_member("mnt"):
            return self.root.mnt

        raise AttributeError("Unable to find the root mount")


class mm_struct(objects.StructType):

    def get_mmap_iter(self) -> Iterable[interfaces.objects.ObjectInterface]:
        """Returns an iterator for the mmap list member of an mm_struct."""

        if not self.mmap:
            return

        yield self.mmap

        seen = {self.mmap.vol.offset}
        link = self.mmap.vm_next

        while link != 0 and link.vol.offset not in seen:
            yield link
            seen.add(link.vol.offset)
            link = link.vm_next


class super_block(objects.StructType):
    # include/linux/kdev_t.h
    MINORBITS = 20

    # Superblock flags
    SB_RDONLY = 1            # Mount read-only
    SB_NOSUID = 2            # Ignore suid and sgid bits
    SB_NODEV = 4             # Disallow access to device special files
    SB_NOEXEC = 8            # Disallow program execution
    SB_SYNCHRONOUS = 16      # Writes are synced at once
    SB_MANDLOCK = 64         # Allow mandatory locks on an FS
    SB_DIRSYNC = 128         # Directory modifications are synchronous
    SB_NOATIME = 1024        # Do not update access times
    SB_NODIRATIME = 2048     # Do not update directory access times
    SB_SILENT = 32768
    SB_POSIXACL = (1 << 16)   # VFS does not apply the umask
    SB_KERNMOUNT = (1 << 22)  # this is a kern_mount call
    SB_I_VERSION = (1 << 23)  # Update inode I_version field
    SB_LAZYTIME = (1 << 25)   # Update the on-disk [acm]times lazily

    SB_OPTS = {
        SB_SYNCHRONOUS: "sync",
        SB_DIRSYNC: "dirsync",
        SB_MANDLOCK: "mand",
        SB_LAZYTIME: "lazytime"
    }

    @property
    def major(self) -> int:
        return self.s_dev >> self.MINORBITS

    @property
    def minor(self) -> int:
        return self.s_dev & ((1 << self.MINORBITS) - 1)

    def get_flags_access(self) -> str:
        return 'ro' if self.s_flags & self.SB_RDONLY else 'rw'

    def get_flags_opts(self) -> Iterable[str]:
        sb_opts = [self.SB_OPTS[sb_opt] for sb_opt in self.SB_OPTS if sb_opt & self.s_flags]
        return sb_opts

    def get_type(self):
        mnt_sb_type = utility.pointer_to_string(self.s_type.name, count=255)
        if self.s_subtype:
            mnt_sb_subtype = utility.pointer_to_string(self.s_subtype, count=255)
            mnt_sb_type += "." + mnt_sb_subtype
        return mnt_sb_type


class vm_area_struct(objects.StructType):
    perm_flags = {
        0x00000001: "r",
        0x00000002: "w",
        0x00000004: "x",
    }

    extended_flags = {
        0x00000001: "VM_READ",
        0x00000002: "VM_WRITE",
        0x00000004: "VM_EXEC",
        0x00000008: "VM_SHARED",
        0x00000010: "VM_MAYREAD",
        0x00000020: "VM_MAYWRITE",
        0x00000040: "VM_MAYEXEC",
        0x00000080: "VM_MAYSHARE",
        0x00000100: "VM_GROWSDOWN",
        0x00000200: "VM_NOHUGEPAGE",
        0x00000400: "VM_PFNMAP",
        0x00000800: "VM_DENYWRITE",
        0x00001000: "VM_EXECUTABLE",
        0x00002000: "VM_LOCKED",
        0x00004000: "VM_IO",
        0x00008000: "VM_SEQ_READ",
        0x00010000: "VM_RAND_READ",
        0x00020000: "VM_DONTCOPY",
        0x00040000: "VM_DONTEXPAND",
        0x00080000: "VM_RESERVED",
        0x00100000: "VM_ACCOUNT",
        0x00200000: "VM_NORESERVE",
        0x00400000: "VM_HUGETLB",
        0x00800000: "VM_NONLINEAR",
        0x01000000: "VM_MAPPED_COP__VM_HUGEPAGE",
        0x02000000: "VM_INSERTPAGE",
        0x04000000: "VM_ALWAYSDUMP",
        0x08000000: "VM_CAN_NONLINEAR",
        0x10000000: "VM_MIXEDMAP",
        0x20000000: "VM_SAO",
        0x40000000: "VM_PFN_AT_MMAP",
        0x80000000: "VM_MERGEABLE",
    }

    def _parse_flags(self, vm_flags, parse_flags) -> str:
        """Returns an string representation of the flags in a
        vm_area_struct."""

        retval = ""

        for mask, char in parse_flags.items():
            if (vm_flags & mask) == mask:
                retval = retval + char
            else:
                retval = retval + '-'

        return retval

    # only parse the rwx bits
    def get_protection(self) -> str:
        return self._parse_flags(self.vm_flags & 0b1111, vm_area_struct.perm_flags)

    # used by malfind
    def get_flags(self) -> str:
        return self._parse_flags(self.vm_flags, self.extended_flags)

    def get_page_offset(self) -> int:
        if self.vm_file == 0:
            return 0

        return self.vm_pgoff << constants.linux.PAGE_SHIFT

    def get_name(self, context, task):
        if self.vm_file != 0:
            fname = linux.LinuxUtilities.path_for_file(context, task, self.vm_file)
        elif self.vm_start <= task.mm.start_brk and self.vm_end >= task.mm.brk:
            fname = "[heap]"
        elif self.vm_start <= task.mm.start_stack <= self.vm_end:
            fname = "[stack]"
        elif self.vm_mm.context.has_member("vdso") and self.vm_start == self.vm_mm.context.vdso:
            fname = "[vdso]"
        else:
            fname = "Anonymous Mapping"

        return fname

    # used by malfind
    def is_suspicious(self):
        ret = False

        flags_str = self.get_protection()

        if flags_str == "rwx":
            ret = True

        elif flags_str == "r-x" and self.vm_file.dereference().vol.offset == 0:
            ret = True

        return ret


class qstr(objects.StructType):

    def name_as_str(self) -> str:
        if self.has_member("len"):
            str_length = self.len + 1  # Maximum length should include null terminator
        else:
            str_length = 255

        try:
            ret = objects.utility.pointer_to_string(self.name, str_length)
        except (exceptions.InvalidAddressException, ValueError):
            ret = ""

        return ret


class dentry(objects.StructType):

    def path(self) -> str:
        """Based on __dentry_path Linux kernel function"""
        reversed_path = []
        dentry_seen = set()
        current_dentry = self
        while (not current_dentry.is_root() and
               current_dentry.vol.offset not in dentry_seen):
            parent = current_dentry.d_parent
            reversed_path.append(current_dentry.d_name.name_as_str())
            dentry_seen.add(current_dentry.vol.offset)
            current_dentry = parent
        return "/" + "/".join(reversed(reversed_path))

    def is_root(self) -> bool:
        return self.vol.offset == self.d_parent

    def is_subdir(self, old_dentry):
        """Is this dentry a subdirectory of old_dentry?

        Returns true if this dentry is a subdirectory of the parent (at any depth).
        Otherwise, it returns false.
        """
        if self.vol.offset == old_dentry:
            return True

        return self.d_ancestor(old_dentry)

    def d_ancestor(self, ancestor_dentry):
        """Search for an ancestor

        Returns the ancestor dentry which is a child of "ancestor_dentry",
        if "ancestor_dentry" is an ancestor of "child_dentry", else None.
        """

        dentry_seen = set()
        current_dentry = self
        while (not current_dentry.is_root() and
               current_dentry.vol.offset not in dentry_seen):
            if current_dentry.d_parent == ancestor_dentry.vol.offset:
                return current_dentry

            dentry_seen.add(current_dentry.vol.offset)
            current_dentry = current_dentry.d_parent

        return None


class struct_file(objects.StructType):

    def get_dentry(self) -> interfaces.objects.ObjectInterface:
        if self.has_member("f_dentry"):
            return self.f_dentry
        elif self.has_member("f_path"):
            return self.f_path.dentry
        else:
            raise AttributeError("Unable to find file -> dentry")

    def get_vfsmnt(self) -> interfaces.objects.ObjectInterface:
        if self.has_member("f_vfsmnt"):
            return self.f_vfsmnt
        elif self.has_member("f_path"):
            return self.f_path.mnt
        else:
            raise AttributeError("Unable to find file -> vfs mount")


class list_head(objects.StructType, collections.abc.Iterable):

    def to_list(self,
                symbol_type: str,
                member: str,
                forward: bool = True,
                sentinel: bool = True,
                layer: Optional[str] = None) -> Iterator[interfaces.objects.ObjectInterface]:
        """Returns an iterator of the entries in the list.

        Args:
                symbol_type: Type of the list elements
                member: Name of the list_head member in the list elements
                forward: Set false to go backwards
                sentinel: Whether self is a "sentinel node", meaning it is not embedded in a member of the list
                Sentinel nodes are NOT yielded. See https://en.wikipedia.org/wiki/Sentinel_node for further reference
                layer: Name of layer to read from
        Yields:
            Objects of the type specified via the "symbol_type" argument.

        """
        layer = layer or self.vol.layer_name

        relative_offset = self._context.symbol_space.get_type(symbol_type).relative_child_offset(member)

        direction = 'prev'
        if forward:
            direction = 'next'
        try:
            link = getattr(self, direction).dereference()
        except exceptions.InvalidAddressException:
            return

        if not sentinel:
            yield self._context.object(symbol_type, layer, offset = self.vol.offset - relative_offset)

        seen = {self.vol.offset}
        while link.vol.offset not in seen:

            obj = self._context.object(symbol_type, layer, offset = link.vol.offset - relative_offset)
            yield obj

            seen.add(link.vol.offset)
            try:
                link = getattr(link, direction).dereference()
            except exceptions.InvalidAddressException:
                break

    def __iter__(self) -> Iterator[interfaces.objects.ObjectInterface]:
        return self.to_list(self.vol.parent.vol.type_name, self.vol.member_name)


class files_struct(objects.StructType):

    def get_fds(self) -> interfaces.objects.ObjectInterface:
        if self.has_member("fdt"):
            return self.fdt.fd.dereference()
        elif self.has_member("fd"):
            return self.fd.dereference()
        else:
            raise AttributeError("Unable to find files -> file descriptors")

    def get_max_fds(self) -> interfaces.objects.ObjectInterface:
        if self.has_member("fdt"):
            return self.fdt.max_fds
        elif self.has_member("max_fds"):
            return self.max_fds
        else:
            raise AttributeError("Unable to find files -> maximum file descriptors")


class mount(objects.StructType):

    MNT_NOSUID = 0x01
    MNT_NODEV = 0x02
    MNT_NOEXEC = 0x04
    MNT_NOATIME = 0x08
    MNT_NODIRATIME = 0x10
    MNT_RELATIME = 0x20
    MNT_READONLY = 0x40
    MNT_SHRINKABLE = 0x100
    MNT_WRITE_HOLD = 0x200
    MNT_SHARED = 0x1000
    MNT_UNBINDABLE = 0x2000

    MNT_FLAGS = {
        MNT_NOSUID: "nosuid",
        MNT_NODEV: "nodev",
        MNT_NOEXEC: "noexec",
        MNT_NOATIME: "noatime",
        MNT_NODIRATIME: "nodiratime",
        MNT_RELATIME: "relatime",
    }

    def get_mnt_sb(self):
        if self.has_member("mnt"):
            return self.mnt.mnt_sb
        elif self.has_member("mnt_sb"):
            return self.mnt_sb
        else:
            raise AttributeError("Unable to find mount -> super block")

    def get_mnt_root(self):
        if self.has_member("mnt"):
            return self.mnt.mnt_root
        elif self.has_member("mnt_root"):
            return self.mnt_root
        else:
            raise AttributeError("Unable to find mount -> mount root")

    def get_mnt_flags(self):
        if self.has_member("mnt"):
            return self.mnt.mnt_flags
        elif self.has_member("mnt_flags"):
            return self.mnt_flags
        else:
            raise AttributeError("Unable to find mount -> mount flags")

    def get_mnt_parent(self):
        return self.mnt_parent

    def get_mnt_mountpoint(self):
        return self.mnt_mountpoint

    def get_flags_access(self) -> str:
        return "ro" if self.get_mnt_flags() & self.MNT_READONLY else "rw"

    def get_flags_opts(self) -> Iterable[str]:
        flags = [self.MNT_FLAGS[mntflag] for mntflag in self.MNT_FLAGS if mntflag & self.get_mnt_flags()]
        return flags

    def is_shared(self) -> bool:
        return self.get_mnt_flags() & self.MNT_SHARED

    def is_unbindable(self) -> bool:
        return self.get_mnt_flags() & self.MNT_UNBINDABLE

    def is_slave(self) -> bool:
        return self.mnt_master and self.mnt_master.vol.offset != 0

    def get_devname(self) -> str:
        return utility.pointer_to_string(self.mnt_devname, count=255)

    def has_parent(self) -> bool:
        return self.vol.offset != self.mnt_parent

    def get_dominating_id(self, root) -> int:
        """Get ID of closest dominating peer group having a representative under the given root."""
        mnt_seen = set()
        current_mnt = self.mnt_master
        while (current_mnt and
               current_mnt.vol.offset != 0 and
               current_mnt.vol.offset not in mnt_seen):
            peer = current_mnt.get_peer_under_root(self.mnt_ns, root)
            if peer and peer.vol.offset != 0:
                return peer.mnt_group_id

            mnt_seen.add(current_mnt.vol.offset)
            current_mnt = current_mnt.mnt_master
        return 0

    def get_peer_under_root(self, ns, root):
        """Return true if path is reachable from root.
        It mimics the kernel function is_path_reachable(), ref: fs/namespace.c
        """
        mnt_seen = set()
        current_mnt = self
        while current_mnt.vol.offset not in mnt_seen:
            if current_mnt.mnt_ns == ns and current_mnt.is_path_reachable(current_mnt.mnt.mnt_root, root):
                return current_mnt

            mnt_seen.add(current_mnt.vol.offset)
            current_mnt = current_mnt.next_peer()
            if current_mnt.vol.offset == self.vol.offset:
                break

        return None

    def is_path_reachable(self, current_dentry, root):
        """Return true if path is reachable.
        It mimics the kernel function with same name, ref fs/namespace.c:
        """
        mnt_seen = set()
        current_mnt = self
        while (current_mnt.mnt.vol.offset != root.mnt and
               current_mnt.has_parent() and
               current_mnt.vol.offset not in mnt_seen):

            current_dentry = current_mnt.mnt_mountpoint
            mnt_seen.add(current_mnt.vol.offset)
            current_mnt = current_mnt.mnt_parent

        return current_mnt.mnt.vol.offset == root.mnt and current_dentry.is_subdir(root.dentry)

    def next_peer(self):
        table_name = self.vol.type_name.split(constants.BANG)[0]
        mount_struct = "{0}{1}mount".format(table_name, constants.BANG)
        offset = self._context.symbol_space.get_type(mount_struct).relative_child_offset("mnt_share")

        return self._context.object(mount_struct, self.vol.layer_name, offset=self.mnt_share.next.vol.offset - offset)

class vfsmount(objects.StructType):

    def is_valid(self):
        return self.get_mnt_sb() != 0 and \
               self.get_mnt_root() != 0 and \
               self.get_mnt_parent() != 0

    def _get_real_mnt(self):
        table_name = self.vol.type_name.split(constants.BANG)[0]
        mount_struct = f"{table_name}{constants.BANG}mount"
        offset = self._context.symbol_space.get_type(mount_struct).relative_child_offset("mnt")

        return self._context.object(mount_struct, self.vol.layer_name, offset = self.vol.offset - offset)

    def get_mnt_parent(self):
        if self.has_member("mnt_parent"):
            return self.mnt_parent
        else:
            return self._get_real_mnt().mnt_parent

    def get_mnt_mountpoint(self):
        if self.has_member("mnt_mountpoint"):
            return self.mnt_mountpoint
        else:
            return self._get_real_mnt().mnt_mountpoint

    def get_mnt_root(self):
        return self.mnt_root


class kobject(objects.StructType):

    def reference_count(self):
        refcnt = self.kref.refcount
        if self.has_member("counter"):
            ret = refcnt.counter
        else:
            ret = refcnt.refs.counter

        return ret

class mnt_namespace(objects.StructType):
    def get_inode(self):
        if self.has_member("proc_inum"):
            return self.proc_inum
        elif self.ns.has_member("inum"):
            return self.ns.inum
        else:
            raise AttributeError("Unable to find mnt_namespace inode")

    def get_mount_points(self):
        table_name = self.vol.type_name.split(constants.BANG)[0]
        mnt_type = table_name + constants.BANG + "mount"
        if not self._context.symbol_space.has_type(mnt_type):
            # Old kernels ~ 2.6
            mnt_type = table_name + constants.BANG + "vfsmount"

        for mount in self.list.to_list(mnt_type, "mnt_list"):
            yield mount
