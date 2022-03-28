
{{ fullname | escape | underline}}
===============

.. automodule:: {{ fullname }}
   :members:

{% block modules %}
{% if fullname != "temporalio.worker" %}
{% if modules %}
.. autosummary::
   :toctree:
   :template: custom_module_template.rst
   :recursive:
{% for item in modules %}
   {{ item }}
{%- endfor %}
{% endif %}
{% endif %}
{% endblock %}
