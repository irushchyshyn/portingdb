#!/bin/bash

# Update portingdb data
# Run with: $ ./scripts/update_portingdb.sh 

confirm () {
    # Alert the user what they are about to do.
    echo "About to: $@"
    # Ask if they wish to continue.
    read -r -p "Continue? [y/N] " response
    case $response in
        [yY][eE][sS]|[yY])
            # If yes, then go on.
            ;;
        [nN][oO]|[nN])
            # If no, exit.
            echo "Bye!"
            exit
            ;;
        *)
            # Or ask again.
            echo "Wat again? :/"
            confirm $@
            
    esac
}

echo "----------------------- Step 1 ----------------------------"
confirm "Get the Python 3 porting status using 'py3query' dnf plugin"
dnf-3 --disablerepo='*' --enablerepo=rawhide --enablerepo=rawhide-source py3query --refresh -o data/fedora.json

echo -e "\n----------------------- Step 2 ----------------------------"
confirm "Get historical status data"
python3 -u scripts/get-history.py --update data/history.csv | tee history.csv

confirm "Update 'history.csv' file"
mv history.csv data/history.csv

# confirm "Get historical status data for naming"
# python3 -u scripts/get-history.py -n --update data/history-naming.csv | tee history-naming.csv

# confirm "Update 'history-naming.csv' file"
# mv history-naming.csv data/history-naming.csv

echo -e "\n---------------------- Step 3 ----------------------------"
confirm "Load the newly generated data into the database"
python3 -m portingdb -v --datadir=data/ load

echo -e "\n---------------------- Step 4 ----------------------------"
confirm "Compare statuses of packages across two JSON files"
python3 scripts/jsondiff.py <(git show HEAD:data/fedora.json) data/fedora.json
echo "ACTION REQUIRED: Take a closer look at the above output!"

echo -e "\n---------------------- Step 5 ----------------------------"
confirm "Commit changes"
git add data/history.csv data/fedora.json && git commit -m 'Update Fedora data'

confirm "Push changes"
git push origin master
