
.. code-block:: python

    # Plot all the tables directly downstream from ``seq.Genome``:
    (dj.Diagram(seq.Genome)+1).draw()

.. code-block:: python

    # Plot all the tables directly upstream from ``seq.Genome``:
    (dj.Diagram(seq.Genome)-1).draw()

.. code-block:: python

    # Plot the local neighborhood of ``seq.Genome``
    (dj.Diagram(seq.Genome)+1-1+1-1).draw()
