"""
Check for orphaned or dropped packages in yaml data.
"""
import json
import yaml


FEDORA_JSON = 'data/fedora.json'
FEDORA_UPDATE_YAML = 'data/fedora-update.yaml'
GROUPS_YAML = 'data/groups.yaml'
UPSTREAM_YAML = 'data/upstream.yaml'


def check_groups(fedora_json):
    print('--- Result for {} ---'.format(GROUPS_YAML))
    with open(GROUPS_YAML) as f:
        data = yaml.load(f)

    for group in data.values():
        for package in group.get('packages'):
            if package not in fedora_json:
                print(package)


def compare_keys(yaml_file, fedora_json):
    print('--- Result for {} ---'.format(yaml_file))
    with open(yaml_file) as f:
        data = yaml.load(f)

    for package in data:
        if (package not in fedora_json and
                data[package].get('status') != 'dropped'):
            print(package)


def main():
    with open(FEDORA_JSON) as f:
        fedora_json = json.load(f)

    for yaml_file in (UPSTREAM_YAML, FEDORA_UPDATE_YAML):
        compare_keys(yaml_file, fedora_json)

    check_groups(fedora_json)


if __name__ == '__main__':
    main()
