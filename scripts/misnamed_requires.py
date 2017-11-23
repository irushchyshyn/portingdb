#!/usr/bin/python3
"""
This script will take packages from portingdb which are marked
as Misnamed Requires (or a specific list of package names from a file)
and:

- clone the spec file to the temp directory
- fix the misnamed requirements
- build an srpm
- run a mock build
- run a Koji scratch build
- create a fork of the repo in Pagure (for a specified user)
- push changes to a fork
- create a Pagure Pull Request from fork to upstream

Notes:
- For Koji scratch build and to work and to create a PR
  you'll have to be "kinited" with your FAS
- To create a fork, you will need to provide your Pagure
  username and api key (can be created on Pagure UI on user settings)

P.S. The script is a mess.
"""

import logging
import os
import re
import pathlib
import tempfile
import time
import shutil
import subprocess

import click

from libpagure import Pagure
from libpagure.exceptions import APIError
from sqlalchemy import create_engine

from portingdb.htmlreport import get_naming_policy_progress
from portingdb.load import get_db


logging.basicConfig(format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

RHEL_MARKERS = ('{?rhel}', '{?el6}', '{?epel7}')
REQUIRES_PATTERN = '#?\s*(Build)?Requires:\s+(.*)'
REPLACE_PATTERNS = {
    # pattern: (what to replace, how to replace)
    '^python-\w*': ('python-', 'python2-'),
    '[!/]*-python-\w*': ('-python-', 'python2-'),
    '[!/]*-python,?$': ('-python', 'python2-'),
    '^python,?$': ('python', 'python2'),
    # Special cases are sometimes special enough.
    '^PyYAML,?$': ('PyYAML', 'python2-pyyaml'),
}

PAGURE_INSTANCE = 'https://src.fedoraproject.org'
FINALIZING_DOC = 'https://fedoraproject.org/wiki/FinalizingFedoraSwitchtoPython3'

COMMIT = 'Fix misnamed Python 2 dependencies declarations'
COMMENT = f'{COMMIT}\n  (See {FINALIZING_DOC})'

PR_DESCRIPTION = (
    'This package uses names with ambiguous `python-` prefix in requirements.\n\n'
    'According  to Fedora Packaging guidelines for Python [0], '
    'packages must use names with either `python2-` or `python3-` '
    'prefix in requirements where available.\n\n'
    'This PR is part of Fedora\'s Switch to Python 3 effort [1] '
    'aiming to fix misnamed dependencies declarations across Python packages.\n\n'
    'Note that, although this PR was created automatically, any comments or issues which '
    'you might find with it during the review will be fixed.'
    'The PR will remain open for review for a week, and '
    'if no feedback received will be merged.\n\n'
    '[0] https://fedoraproject.org/wiki/Packaging:Python#Dependencies\n'
    f'[1] {FINALIZING_DOC}'
)


def get_portingdb(db):
    """Return session object for portingdb."""
    url = 'sqlite:///' + db
    engine = create_engine(url)
    return get_db(None, engine=engine)


def is_unversioned(name):
    """Check whether unversioned python prefix is used
    in the name (e.g. python-foo).
    """
    if (os.path.isabs(name) or  # is an executable
            os.path.splitext(name)[1]):  # has as extension
        return False

    return (
        name.startswith('python-') or
        '-python-' in name or
        name.endswith('-python') or
        name == 'python')


def has_epel_branch(package_name):
    pagure = Pagure(
        pagure_repository=package_name,
        instance_url=PAGURE_INSTANCE)
    request_url = f"{pagure.instance}/api/0/rpms/{pagure.repo}/git/branches"
    return_value = pagure._call_api(request_url)
    return 'el6' in return_value["branches"] or 'epel7' in return_value["branches"]


# Fixers.
def fix_requires_line(requires):
    requires = re.split('(\s)', requires)
    modified_requires = []

    for require in requires:
        for pattern, (replace_what, replace_with) in REPLACE_PATTERNS.items():
            if re.match(pattern, require):
                require = require.replace(
                    replace_what,
                    replace_with)
        modified_requires.append(require)

    return ''.join(modified_requires)


def fix_spec(spec):
    modified_spec = []

    for index, line in enumerate(spec.split('\n')):
        match = re.match(REQUIRES_PATTERN, line)
        if match:
            for requires in match.groups():
                if not requires or requires == 'Build':
                    continue

                modified_requires = fix_requires_line(requires)

                if requires != modified_requires:
                    line = line.replace(requires, modified_requires)

        modified_spec.append(line)

    modified_spec = '\n'.join(modified_spec)
    return modified_spec


def fix_specfile(specfile, write=True):
    with open(specfile, 'rt') as f:
        spec = f.read()

    new_spec = fix_spec(spec)

    if not write:
        return new_spec
    with open(specfile, 'wt') as out:
        out.write(new_spec)


# Testing.
def test_new_spec(package_dirname):
    """Test the modified spec file:
    - create srpm
    - run a mock build and check results
    - run a Koji build

    Return: a link to Koji scratch build
    """
    # Create srpm.
    subprocess.call(['fedpkg', 'srpm'], cwd=package_dirname, stdout=subprocess.PIPE)
    # Locate the srpm file.
    srpm, = package_dirname.glob('*.src.rpm')

    build_in_mock(srpm)
    koji_scratch_build = build_in_koji(srpm)
    return koji_scratch_build


def build_in_mock(srpm):
    """
    - build in mock for rawhide
    - check that the resulted RPMs do not have any python-foo requirements
    """
    logger.debug('Running mock build')
    try:
        subprocess.check_output(['mock', '-q', '-r', 'fedora-rawhide-x86_64', srpm])
    except subprocess.CalledProcessError as err:
        logger.error(f'Mock build did not pass for {srpm}. Error: {err.output}')
        raise err
    else:
        logger.debug('Mock build completed. Checking resulting rpms')
        # Check that no rpms require python-smth.
        result_dir = pathlib.Path('/var/lib/mock/fedora-rawhide-x86_64/result')  # Can be smth else.
        result_rpms = result_dir.glob('*.rpm')
        for rpm_file in result_rpms:
            requires = subprocess.check_output(['rpm', '-qRp', rpm_file])
            for require in requires.split():
                if is_unversioned(str(require)):
                    logger.error(f'{require} is still not versioned')
                    raise Exception(f'{require} is still not versioned')


def build_in_koji(srpm):
    """
    """
    logger.debug('Running a koji build')

    try:
        output = subprocess.check_output(
            ['koji', 'build', '--scratch', '--noprogress', 'rawhide', srpm])
    except subprocess.CalledProcessError as err:
        logger.error(f'Koji scratch build {srpm}. Error: {err.output}')
        raise err
    else:
        logger.debug(f'Koji scratch build completed. Output: {output}')
        if 'completed successfully' in str(output):
            # Yay Koji build completed. Find a link to a task.
            task_info_re = r'Task info: (https://koji.fedoraproject.org/koji/taskinfo\?taskID=\d+)\\n'
            koji_scratch_build = re.search(task_info_re, str(output)).group(1)
            return koji_scratch_build
        else:
            raise Exception(
                'Seems like Koji scratch build did not complete successfully. '
                f'Output: {output}')


# Pagure integration.
def fork(package_name, pagure_token, pagure_user):
    """Fork a repo in Pagure if not forked yet.

    Return: SSH Source Git URL.
    """
    pagure = Pagure(
        pagure_repository=package_name,
        instance_url=PAGURE_INSTANCE,
        pagure_token=pagure_token
    )
    logger.debug(f"Creating fork of {package_name} for user {pagure_user}")
    url = f"{pagure.instance}/api/0/fork"
    try:
        payload = {'wait': True, 'namespace': 'rpms', 'repo': package_name}
        response = pagure._call_api(url, method='POST', data=payload)
        logger.debug(f"Fork created: {response}")
    except APIError as err:
        if 'already exists' in str(err):
            logger.info(f'User {pagure_user} already has a fork of {package_name}')
        else:
            raise err

    # Get ssh git url.
    url = f"{pagure.instance}/api/0/fork/{pagure_user}/rpms/{package_name}/git/urls"
    return_value = pagure._call_api(url)
    return return_value['urls']['ssh']


def push_to_fork(fork_url, path_to_repo, pagure_user):
    """Commit and push local changes to fork.

    Also check if the commit to fix requires is
    already the last commit, then amend and force push
    to update.
    """
    logger.debug(f"Pushing to a fork {fork_url}")
    try:
        subprocess.check_output(
            ['git', 'remote', 'add', pagure_user, fork_url],
            cwd=path_to_repo,
            stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        if 'already exists' in err.output:
            logger.INFO('Fork already added to remotes')
        else:
            raise err

    subprocess.check_output(
        ['git', 'add', '*.spec'], cwd=path_to_repo, stderr=subprocess.STDOUT)
    subprocess.check_output(
        ['git', 'commit', '-m', COMMIT], cwd=path_to_repo,
        stderr=subprocess.STDOUT)

    # On Pagure you can not immediately push to the fork.
    # And there is no api call to check that the fork is ready.
    # So here is a hack: try o do it at least 4 times with an interval
    # in 3 minutes. Oh well.
    for attempt in range(4):
        try:
            logger.debug(f'Trying to push changes to fork (Attempt {attempt})')
            subprocess.check_output(
                ['git', 'push', '-f', pagure_user, 'master'], cwd=path_to_repo,
                stderr=subprocess.STDOUT)
            break
        except subprocess.CalledProcessError as err:
            if 'DENIED by fallthru' in err.output:
                time.sleep(60 * 3)
    else:
        raise Exception('Could not push to fork, it is still not available')

    logger.debug(f"Successfully pushed to a fork {fork_url}")


def create_pull_request(package_name, koji_scratch_build,
                        pagure_token, pagure_user,
                        fas_user, fas_password):
    """Create a pull request from a fork to upstream.

    Only if not created yet.
    Pagure API does not allow this yet, so selenium it is.
    """
    pagure = Pagure(
        pagure_repository=package_name,
        instance_url=PAGURE_INSTANCE,
        pagure_token=pagure_token
    )

    # TODO: check if the PR is not yet there, in case the script
    # is being run the second time for this project.

    url = (
        f'{pagure.instance}/login/?next={pagure.instance}'
        f'/fork/{pagure_user}/rpms/{package_name}/diff/master..master')

    try:
        from selenium import webdriver
    except ImportError:
        logger.error('If you want to create PRs, please install python3-selenium')
        return

    driver = webdriver.Firefox()
    driver.get(url)

    # Sometimes when the user is "kinited", the login page does not open,
    # and you go directly to the PR page.
    if driver.title == 'Login':
        if not fas_user or fas_password:
            raise Exception('Please provide both FAS username and password')
        login_elem = driver.find_element_by_name('login_name')
        login_elem.clear()
        login_elem.send_keys(fas_user)

        password_elem = driver.find_element_by_name('login_password')
        password_elem.clear()
        password_elem.send_keys(fas_password)

        login_button = driver.find_element_by_id('loginbutton')
        login_button.click()

    pr_title = driver.find_element_by_name('title')
    pr_title_value = pr_title.get_attribute('value')

    if pr_title_value != COMMIT:
        # This means the PR either contains more commits or is wrong.
        # Needs to be checked manually.
        raise Exception(
            'Opening the PR did not go well. '
            f'The PR title: {pr_title_value}')

    pr_init_comment = driver.find_element_by_id('initial_comment')
    pr_init_comment.clear()
    pr_init_comment.send_keys(
        PR_DESCRIPTION + f'\n\nKoji scratch build: {koji_scratch_build}')

    create_button = driver.find_element_by_xpath(
        "//input[@type='submit'][@value='Create']")
    # create_button.click()
    import ipdb; ipdb.set_trace()

    # TODO: check the page if it was a success.
    driver.close()


@click.command(help=__doc__)
@click.option('--db', help="Database file path",
              default=os.path.abspath('portingdb.sqlite'))
@click.option('--dirname', help="Directory path to clone packages and do the tests",
              default=None, type=click.Path(exists=True))
@click.option('--cleandir', help="Clean the directory if not empty",
              is_flag=True)
@click.option('-n', help="Number of packages to clone and fix",
              default=None)
@click.option('--user', help="Username to use for change log",
              default=None)
@click.option('--no-test', help="Build srpm and run mock build",
              is_flag=True)
@click.option('--pagure', help="Create pagure fork for project and push to fork",
              is_flag=True)
@click.option('--pagure-token', help="Pagure token to create a fork",
              default=None)
@click.option('--pagure-user', help="Pagure user to operate with",
              default=None)
@click.option('--fas-user', help="FAS username",
              default=None)
@click.option('--fas-password', help="FAS password",
              prompt=True, hide_input=True, confirmation_prompt=False)
@click.option('--packages', help="A file with package names to process (separated py new line)",
              default=None, type=click.Path(exists=True))
def main(db, dirname, cleandir, n, user, no_test, pagure,
         pagure_token, pagure_user, fas_user, fas_password, packages):
    if packages:
        with open(packages, 'rt') as f:
            require_misnamed = f.read().splitlines()
    else:
        db = get_portingdb(db)
        _, data = get_naming_policy_progress(db)
        require_misnamed = [pkg.name for pkg in data[1][1]]

    if not require_misnamed:
        logger.info('No packages with misnamed requires found')

    if not dirname:
        dirname = tempfile.mkdtemp()
    if cleandir:
        shutil.rmtree(f'{dirname}')

    non_fedora_packages = []
    fixed_packages = []
    problem_packages = []

    logger.debug(f'Cloning packages into {dirname}')
    for package_name in require_misnamed:
        # Check if the package has EPEL branches.
        is_epel = has_epel_branch(package_name)

        if is_epel:
            logger.info(
                f'The package {package_name} seems to be built for Fedora '
                'and EPEL. Skipping for now as the spec file may be shared.')
            non_fedora_packages.append(package_name)
            continue

        package_dirname = pathlib.Path(f'{dirname}/{package_name}')
        logger.debug(f'Cloning {package_name} into {package_dirname}')
        subprocess.check_output(
            ['fedpkg', 'clone', package_name, f'{package_dirname}'],
            stderr=open(os.devnull, 'w'))

        # Locate the spec file.
        specfile, = package_dirname.glob('*.spec')

        # Fix the spec file.
        fix_specfile(specfile)

        # Add a change log to the spec file.
        cmd = ['rpmdev-bumpspec', '-c', COMMENT, specfile]
        if user:
            cmd += ['-u', user]
        subprocess.check_call(cmd)

        # Show the diff.
        subprocess.call(['git', '--no-pager', 'diff', specfile], cwd=package_dirname)

        # Testing.
        if no_test:
            logger.info('No testing done')
            koji_scratch_build = ''
        else:
            try:
                koji_scratch_build = test_new_spec(package_dirname)
            except Exception as err:
                logger.error(f'Testing changes for {package_name} failed. Error: {err}')
                problem_packages.append(package_name)
                continue

        if pagure:
            if not pagure_token or not pagure_user:
                raise Exception("Please provide both pagure user and token")

            try:
                fork_url = fork(package_name, pagure_token, pagure_user)
            except Exception as err:
                logger.error(f"Failed to create a fork for {package_name}. Error: {err}")
                problem_packages.append(package_name)
                continue

            try:
                push_to_fork(fork_url, package_dirname, pagure_user)
            except Exception as err:
                logger.error(f"Failed to push to a fork for {package_name}. Error: {err}")
                problem_packages.append(package_name)
                continue

            try:
                create_pull_request(
                    package_name, koji_scratch_build,
                    pagure_token, pagure_user,
                    fas_user, fas_password)
            except Exception as err:
                logger.error(f"Failed to create a PR for {package_name}. Error: {err}")
                problem_packages.append(package_name)
                continue

        fixed_packages.append(package_name)

    result = (
        '\n\nRESULTS:\n'
        f'The following {len(non_fedora_packages)} were skipped,'
        f' because they are also built for EPEL:\n{non_fedora_packages}\n'
        f'The following {len(problem_packages)} packages had problem'
        f' while testing and were not pushed:\n{problem_packages}\n.'
        f'The following {len(fixed_packages)} packages were successfully fixed:\n'
        f'{fixed_packages}\n.'
    )
    logger.info(result)


if __name__ == '__main__':
    main()
