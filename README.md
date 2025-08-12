# FuTURES Lab Website

## Updating Bug Lists

1. Navigate to the `bugs` folder.
2. Create (or update) file `FirstnameLastname.txt` containing URLs of your discovered bugs.
3. Run `bugs2json.py FirstnameLastname.txt`, which will auto-generate JSON file `FirstnameLastname.json`.
4. Update `bugs/index.html` to include your `FirstnameLastname.json` in the `const jsonFiles` list.
5. Check your generated JSON file for any errors. 
	* It may be necessary to extend `bugs2json.py` with repo-specific handling (e.g., for QCAD). 
	* If you do this, please open a pull request with your proposed changes.
6. Test things out locally by running `python3 -m http.server`.