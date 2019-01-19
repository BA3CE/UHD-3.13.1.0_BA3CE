#
# Copyright 2015 Ettus Research
#

if [expr $argc < 2] {
    error "ERROR: Invalid number of arguments"
    exit
}

set cmd       [lindex $argv 0]
set part_name [lindex $argv 1]

create_project -in_memory -ip -name inmem_ip_proj -part $part_name
if { [string compare $cmd "create"] == 0 } {
    if [expr $argc < 5] {
        error "ERROR: Invalid number of arguments for the create operation"
        exit
    }
    set ip_name [lindex $argv 2]
    set ip_dir  [lindex $argv 3]
    set ip_vlnv [lindex $argv 4]
    create_ip -vlnv $ip_vlnv -module_name $ip_name -dir $ip_dir
} elseif { [string compare $cmd "modify"] == 0 } {
    if [expr $argc < 3] {
        error "ERROR: Invalid number of arguments for the modify operation"
        exit
    }

    set src_file [lindex $argv 2]
    set src_ext [file extension $src_file ]
    if [expr [lsearch {.xci} $src_ext] >= 0] {
        read_ip $src_file
    } elseif [expr [lsearch {.bd} $src_ext] >= 0] {
        add_files -norecurse $src_file
        export_ip_user_files -of_objects  [get_files $src_file] -force -quiet
        open_bd_design $src_file
    } else {
        puts "ERROR: Invalid file extension: $src_ext"
    }

} elseif { [string compare $cmd "list"] == 0 } {
    puts "Supported IP for device ${part_name}:"
    foreach ip [lsort [get_ipdefs]] {
        puts "- $ip"
    }
} elseif { [string compare $cmd "upgrade"] == 0 } {
    if [expr $argc < 3] {
        error "ERROR: Invalid number of arguments for the upgrade operation"
        exit
    }
    set src_file [lindex $argv 2]
    read_ip $src_file
    upgrade_ip [get_ips *]
} else {
    error "ERROR: Invalid command: $cmd"
}