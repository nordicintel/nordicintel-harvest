# Initial provider import

Source: nordicintel-backend `35b7310`, PROVIDERS.json. This is a one-time import, not runtime configuration. Existing providers are never replaced by the import command.

Labels/descriptions retain their declared default language. Database mapping keys become database_ids. Used base_web_url, cell_limit and max_concurrency settings are kept under config.extension. variable_limit and data_formats are unused by extracted adapters and omitted. Legacy db labels, api_status and source_references are not execution inputs; consult source provenance rather than creating a parallel configuration format.
